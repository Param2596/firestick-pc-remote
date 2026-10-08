#!/system/bin/sh
# Called directly as: bridge-session.sh direct /dev/input/eventN
# Called by inotifyd as: bridge-session.sh EVENTS /dev/input eventN
if [ "$1" = "direct" ]; then
  dev=$2
else
  case "$1" in
    *n*) dev="$2/$3" ;;
    *) exit 0 ;;
  esac
fi

hit=/data/local/tmp/bridge.hit
rm -f "$hit"
getevent -pl "$dev" 2>/dev/null | while IFS= read -r line; do
  case "$line" in
    *"AR Keyboard"*)
      echo yes > "$hit"
      ;;
  esac
done
[ -f "$hit" ] || exit 0
rm -f "$hit"
echo "remote $dev" >>/data/local/tmp/bridge.log

delay=2
while [ -e "$dev" ]; do
  if toybox nc -w 2 -z "$PC" "$PORT" >/dev/null 2>&1; then
    echo "grab $dev" >>/data/local/tmp/bridge.log
    {
      printf '%s\n' "$TOKEN"
      "$GRAB" "$dev" &
      grab=$!
      while kill -0 "$grab" 2>/dev/null; do
        sleep 20
        printf '.\n' || break
      done
      kill "$grab" 2>/dev/null
      wait "$grab" 2>/dev/null
    } | toybox nc -q 1 "$PC" "$PORT"
    echo "released $dev" >>/data/local/tmp/bridge.log
    delay=2
  fi
  [ -e "$dev" ] || break
  sleep "$delay"
  if [ "$delay" -lt 20 ]; then
    delay=$((delay + 3))
  fi
done
echo "remote gone" >>/data/local/tmp/bridge.log
