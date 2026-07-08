"""
Scheduler - Runs the agent periodically on a schedule.
Fetches new calls every N hours automatically.

Run: python scheduler.py
     python scheduler.py --interval 3600  (run every hour)
     python scheduler.py --interval 43200 (run every 12 hours)
"""
import sys
import time
import argparse
from datetime import datetime
import schedule
from dotenv import load_dotenv

from settings import get_settings
from logging_config import setup_logging
from run_flow import main as run_pipeline

logger = None


def log_start():
    """Log scheduler start."""
    logger.info("=" * 70)
    logger.info(f"Scheduler started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)


def job_wrapper():
    """Wrapper to run pipeline and handle errors."""
    logger.info("")
    logger.info("╔════════════════════════════════════════════════════════════════════╗")
    logger.info(f"║ Running pipeline: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("╚════════════════════════════════════════════════════════════════════╝")

    try:
        exit_code = run_pipeline()
        if exit_code == 0:
            logger.info(f"✓ Pipeline succeeded (exit code {exit_code})")
        elif exit_code == 2:
            logger.warning(f"! No calls found (exit code {exit_code})")
        else:
            logger.error(f"X Pipeline failed (exit code {exit_code})")
        return exit_code
    except Exception as e:
        logger.exception(f"Scheduler job failed: {e}")
        return 1
    finally:
        next_run = schedule.next_run()
        if next_run:
            logger.info(f"Next run: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("")


def format_interval(seconds):
    """Convert seconds to human-readable format."""
    hours = seconds / 3600
    if hours >= 1:
        return f"{int(hours)} hour(s)"
    else:
        minutes = seconds / 60
        return f"{int(minutes)} minute(s)"


def main():
    """Run the scheduler."""
    global logger

    # Parse arguments
    parser = argparse.ArgumentParser(description="Schedule agent to run periodically")
    parser.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="Run interval in seconds (default: 3600 = 1 hour)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't schedule)",
    )
    args = parser.parse_args()

    # Validate interval
    if args.interval <= 0:
        print(f"Error: --interval must be positive (got {args.interval})")
        return 1

    # Setup
    load_dotenv()
    try:
        settings = get_settings()
        logger = setup_logging(settings.LOG_LEVEL)
    except ValueError as e:
        logger = setup_logging()
        logger.error(str(e))
        return 1

    log_start()

    # Single run mode
    if args.once:
        logger.info("Running once (--once flag set)...")
        return job_wrapper()

    # Schedule mode
    interval_readable = format_interval(args.interval)
    logger.info(f"Scheduling pipeline to run every {interval_readable}")
    logger.info(f"Interval: {args.interval} seconds")
    logger.info("")
    logger.info("Press Ctrl+C to stop the scheduler")
    logger.info("")

    # Schedule the job
    schedule.every(args.interval).seconds.do(job_wrapper)

    # Run first time immediately
    logger.info("Running first iteration immediately...")
    job_wrapper()

    # Keep running
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.warning("Scheduler interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
