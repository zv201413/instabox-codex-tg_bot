"""Start validated components, supervise their tasks, exit nonzero on failure."""
import argparse
import asyncio
import logging
import signal
import time
import fcntl
from pathlib import Path
from datetime import datetime, timezone

from controller.agent_api import AgentApiServer
from controller.config import ControllerConfig
from controller.db import Database
from controller.instabox import InstaBoxClient
from controller.scheduler import LeaseScheduler
from controller.telegram_bot import TelegramBot

logger = logging.getLogger('main')


async def supervise(tasks, stopped):
    waiter = asyncio.create_task(stopped.wait())
    try:
        done, _ = await asyncio.wait([*tasks,waiter],return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not waiter:
                if task.cancelled():
                    raise RuntimeError('Required component was cancelled')
                error = task.exception()
                raise RuntimeError(f'Required component exited: {task.get_name()}') from error
    finally:
        waiter.cancel()
        await asyncio.gather(waiter,return_exceptions=True)


async def main_async(config_path=''):
    cfg = ControllerConfig.from_env(config_path or None).validate(required=True)
    Path(cfg.db_path).parent.mkdir(parents=True,exist_ok=True)
    owner_lock = open(cfg.db_path + '.lock','a')
    try:
        fcntl.flock(owner_lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner_lock.close()
        raise RuntimeError('This data volume already has a controller')
    db = Database(cfg.db_path)
    scheduler = LeaseScheduler(cfg,db,InstaBoxClient(cfg.api_url,cfg.api_key))
    await scheduler.initialize()  # Read/target failures must fail startup even with a bot configured.
    bot = TelegramBot(cfg,db,scheduler) if cfg.tg_token else None
    tasks, stopped = [], asyncio.Event()
    def health():
        now = time.monotonic()
        statuses = {task.get_name():not task.done() for task in tasks}
        tick = scheduler.last_tick
        statuses['scheduler_progress'] = tick is not None and (datetime.now(timezone.utc)-tick).total_seconds() < 150
        if bot:
            statuses['telegram_receiving'] = now - bot.last_success < 120
            statuses['telegram_dispatch'] = now - bot.last_dispatch < 150
            statuses['telegram_outbox'] = now - bot.last_outbox < 90
            statuses['telegram_callbacks'] = now - bot.last_callback < 90
        return bool(statuses and all(statuses.values())), statuses
    if bot:
        await bot.initialize()
    api = AgentApiServer(cfg,db,scheduler,health)
    await api.start()  # A bind failure exits immediately.
    loop = asyncio.get_running_loop()
    registered = []
    for sig in (signal.SIGTERM,signal.SIGINT):
        try:
            loop.add_signal_handler(sig,stopped.set)
            registered.append(sig)
        except NotImplementedError:
            pass
    tasks.extend([asyncio.create_task(scheduler.run_loop(),name='scheduler'),asyncio.create_task(api.serve(),name='agent_api')])
    if bot:
        tasks.extend([asyncio.create_task(bot.run_poll_loop(),name='telegram_intake'),
                      asyncio.create_task(bot.run_dispatch_loop(),name='telegram_dispatch'),
                      asyncio.create_task(bot.run_outbox_loop(),name='telegram_outbox'),
                      asyncio.create_task(bot.run_callback_loop(),name='telegram_callbacks')])
    try:
        await supervise(tasks,stopped)
    finally:
        # Cancel work before releasing the lease so an in-flight tick cannot recreate it afterward.
        for task in tasks:
            if task.get_name() != 'scheduler':
                task.cancel()
        if bot:
            await bot.stop()
        await api.stop()
        try:
            await asyncio.wait_for(scheduler.stop(),65)
        except TimeoutError:
            db.update_state(release_requested=1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        owner_lock.close()
        for sig in registered:
            loop.remove_signal_handler(sig)


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('--env',default='')
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.env))
    except KeyboardInterrupt:
        return
    except Exception as error:
        # Print the error type only: arbitrary config paths/exceptions can include secrets.
        logger.error('Controller failed: %s',type(error).__name__)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
