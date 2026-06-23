"""
RabbitMQ topology setup, publisher, and dual consumer (primary + DLQ).
"""
import asyncio
import json
import logging
from typing import TYPE_CHECKING

import aio_pika
import aio_pika.abc

from config.settings import settings

if TYPE_CHECKING:
    from src.target_db import DatabaseManager

logger = logging.getLogger(__name__)


class RabbitMQManager:
    def __init__(self, db_manager: "DatabaseManager") -> None:
        self._db = db_manager
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(
            settings.rabbitmq_url,
            reconnect_interval=5,
        )
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=1)
        logger.info("RabbitMQ connection established.")

    async def setup_topology(self) -> None:
        assert self._channel is not None

        # 1. Dead-letter exchange — fanout: all nacked messages land here
        dlx = await self._channel.declare_exchange(
            settings.dlx_exchange,
            aio_pika.ExchangeType.FANOUT,
            durable=True,
        )

        # 2. Dead Letter Queue bound to the DLX
        dlq = await self._channel.declare_queue(settings.dead_letter_queue, durable=True)
        await dlq.bind(dlx)

        # 3. Primary exchange (direct routing)
        primary_exchange = await self._channel.declare_exchange(
            settings.primary_exchange,
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )

        # 4. Primary queue — any nack(requeue=False) routes to the DLX automatically
        primary_queue = await self._channel.declare_queue(
            settings.primary_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": settings.dlx_exchange,
                "x-message-ttl": 300_000,  # 5-minute safety TTL
            },
        )
        await primary_queue.bind(primary_exchange, routing_key=settings.primary_queue)

        logger.info(
            "Topology ready: %s → %s · DLX: %s → %s",
            settings.primary_exchange, settings.primary_queue,
            settings.dlx_exchange, settings.dead_letter_queue,
        )

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
            logger.info("RabbitMQ connection closed.")

    # ── Publisher ─────────────────────────────────────────────────────────────

    async def publish(self, payload: dict) -> None:
        assert self._channel is not None
        exchange = await self._channel.get_exchange(settings.primary_exchange)
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(payload).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=settings.primary_queue,
        )
        logger.debug("Published event_id=%s to %s", payload.get("event_id"), settings.primary_exchange)

    # ── Consumer entrypoint ───────────────────────────────────────────────────

    async def start_consumers(self) -> None:
        await asyncio.gather(
            self._consume_primary(),
            self._consume_dlq(),
        )

    # ── Primary consumer ──────────────────────────────────────────────────────

    async def _consume_primary(self) -> None:
        assert self._channel is not None
        queue = await self._channel.get_queue(settings.primary_queue)
        async with queue.iterator() as q:
            async for message in q:
                async with message.process(ignore_processed=True):
                    await self._handle_primary_message(message)

    async def _handle_primary_message(
        self, message: aio_pika.abc.AbstractIncomingMessage
    ) -> None:
        try:
            payload = json.loads(message.body)
            logger.info("Primary consumer: event_id=%s", payload.get("event_id", "?"))
            self._db.insert_event(payload)
            await message.ack()
            logger.info("✓ Inserted event_id=%s into DuckDB.", payload.get("event_id"))
        except Exception as exc:
            logger.warning("✗ Schema validation failed (%s) — routing to DLQ.", exc)
            await message.nack(requeue=False)

    # ── DLQ consumer ──────────────────────────────────────────────────────────

    async def _consume_dlq(self) -> None:
        assert self._channel is not None
        queue = await self._channel.get_queue(settings.dead_letter_queue)
        async with queue.iterator() as q:
            async for message in q:
                async with message.process(ignore_processed=True):
                    await self._handle_dlq_message(message)

    async def _handle_dlq_message(
        self, message: aio_pika.abc.AbstractIncomingMessage
    ) -> None:
        from src.healing_agent import HealingAgent

        try:
            payload = json.loads(message.body)
            event_id = payload.get("event_id", "unknown")
            logger.warning("⚕  DLQ received event_id=%s — starting repair loop.", event_id)

            failed_id = self._db.store_failed_message(payload)
            agent = HealingAgent(db_manager=self._db)
            success = await agent.heal(message_id=failed_id, raw_payload=payload)

            if success:
                await message.ack()
                logger.info("✓ Healing complete for event_id=%s — DLQ message ack'd.", event_id)
            else:
                await message.nack(requeue=False)
                logger.error(
                    "✗ Healing failed for event_id=%s — message discarded to prevent loop.",
                    event_id,
                )
        except Exception as exc:
            logger.exception("Unhandled error in DLQ consumer: %s", exc)
            await message.nack(requeue=False)
