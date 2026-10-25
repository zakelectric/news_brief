#!/bin/bash
# Cron entry point (5:00 AM daily). claude lives in ~/.local/bin, which cron's PATH lacks.
export PATH="/home/web/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /home/web/Programs/news_brief || exit 1
PY=/home/web/Programs/venv/bin/python
echo "=== $(date) ===" >> run.log
$PY news_brief.py run >> run.log 2>&1 && $PY news_brief.py publish >> run.log 2>&1
