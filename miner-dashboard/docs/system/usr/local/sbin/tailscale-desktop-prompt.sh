#!/bin/bash
# Desktop-Feld. Abbrechen oder leere Eingabe verwendet den gespeicherten Schlüssel.
DONE=/var/lib/tailscale-firstboot.done
if [ -f "$DONE" ] || [ -f /var/lib/tailscale-joined ]; then
  exit 0
fi
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  exit 0
fi
TOKEN=""
if command -v zenity >/dev/null 2>&1; then
  TOKEN=$(zenity --entry --title="Tailscale" --text="Tailscale-Token. Abbrechen verwendet den gespeicherten Schlüssel." --width=420) || TOKEN=""
else
  TOKEN=$(python3 - <<'PY'
import tkinter as tk
root = tk.Tk()
root.title("Tailscale")
root.geometry("460x110")
tk.Label(root, text="Tailscale-Token. Leer lassen = gespeicherter Schlüssel.").pack(padx=8, pady=(8, 2))
value = tk.StringVar()
entry = tk.Entry(root, textvariable=value, width=48, show="")
entry.pack(padx=8)
entry.focus_set()
def ok(_event=None):
    root.destroy()
entry.bind("<Return>", ok)
tk.Button(root, text="OK", command=ok).pack(pady=6)
root.mainloop()
print(value.get())
PY
) || TOKEN=""
fi
TOKEN=$(printf '%s' "$TOKEN" | tr -d '[:space:]')
if [ -z "$TOKEN" ]; then
  sudo -n /usr/local/sbin/tailscale-token-apply.sh
  exit $?
fi
sudo -n /usr/local/sbin/tailscale-token-apply.sh "$TOKEN"
