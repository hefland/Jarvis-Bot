#!/usr/bin/env bash
# Simple controls for Jarvis.
#   ./manage.sh start      start Jarvis in the background
#   ./manage.sh stop       stop it
#   ./manage.sh restart    restart it (after changing some settings)
#   ./manage.sh status     is it running?
#   ./manage.sh logs       watch what it's doing (press Ctrl+C to quit watching)
#   ./manage.sh settings   open the settings page
#   ./manage.sh update     download the newest version from GitHub and restart
#   ./manage.sh autostart-on / autostart-off
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
SERVICE=jarvis-bot
PORT="$(python3 -c "import json,os;p=os.path.expanduser('~/.config/jarvis-bot/settings.json');print(json.load(open(p))['web']['port'] if os.path.exists(p) else 8765)" 2>/dev/null || echo 8765)"

case "$1" in
  start)    systemctl --user start $SERVICE && echo "Jarvis started." ;;
  stop)     systemctl --user stop $SERVICE && echo "Jarvis stopped." ;;
  restart)  systemctl --user restart $SERVICE && echo "Jarvis restarted." ;;
  status)   systemctl --user status $SERVICE --no-pager ;;
  logs)     journalctl --user -u $SERVICE -f -n 50 ;;
  settings) xdg-open "http://localhost:$PORT" >/dev/null 2>&1 & ;;
  autostart-on)  systemctl --user enable $SERVICE && echo "Jarvis will start when you log in." ;;
  autostart-off) systemctl --user disable $SERVICE && echo "Jarvis won't start by itself anymore." ;;
  update)
    git pull --ff-only || { echo "Couldn't download the update (see message above)."; exit 1; }
    .venv/bin/pip install -q -r requirements.txt
    .venv/bin/pip install -q -U yt-dlp   # YouTube changes often; keep the finder fresh
    command -v mpv >/dev/null 2>&1 && command -v xdotool >/dev/null 2>&1 || sudo apt-get install -y -q mpv playerctl xdotool
    systemctl --user restart $SERVICE && echo "Updated and restarted."
    ;;
  *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
