#!/bin/sh
# Load the ELEGOO key map into the gpio_ir_recv device, whatever its rcN number is.
rc=$(/usr/bin/ir-keytable | awk '/^Found/ {d = $2} /Name: gpio_ir_recv/ {print d; exit}')
rc=$(basename "$rc")
[ -n "$rc" ] || { echo "gpio_ir_recv device not found" >&2; exit 1; }
exec /usr/bin/ir-keytable -s "$rc" -c -p nec -w /etc/rc_keymaps/elegoo-21-keymap
