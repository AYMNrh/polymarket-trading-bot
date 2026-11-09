import argparse
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import threading

from .runner import DEFAULT_DATA, ROOT, run_cycle
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Paper-only complete-set research. No live execution mode.')
    parser.add_argument('command', choices=('once', 'status', 'serve'))
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--port', type=int, default=9091)
    parser.add_argument('--interval', type=int, default=60)
    args = parser.parse_args()
    if args.interval < 30:
        parser.error('interval must be at least 30 seconds')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(args.data_dir / 'ledger.sqlite3')
    if args.command == 'status':
        print(json.dumps(store.snapshot(), indent=2))
        return 0
    if args.command == 'once':
        result = run_cycle(store)
        print(json.dumps({k: result[k] for k in ('status', 'cash', 'realized_pnl', 'report', 'whales')}, indent=2))
        return 0 if result['healthy'] else 1
    from .dashboard import create_app
    import uvicorn
    stop = threading.Event()
    def worker():
        while not stop.is_set():
            try:
                result = run_cycle(store)
                logging.info('cycle: %s %s', result['status'], json.dumps(result['report']))
            except BlockingIOError:
                logging.info('Another cycle holds the lock; skipping.')
            except Exception as exc:
                logging.exception('Paper cycle failed')
                import time
                store.record_scan(time.time(), False, {'errors': [str(exc)]})
            stop.wait(args.interval)
    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=worker, daemon=True, name='paper-cycle')
        thread.start()
        yield
        stop.set()
    uvicorn.run(create_app(store, lifespan, ROOT), host='127.0.0.1', port=args.port, log_level='warning')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
