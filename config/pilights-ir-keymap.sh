#!/bin/sh
# Load the ELEGOO key map into the gpio_ir_recv device, whatever its rcN number is.
for i in $(seq 30); do
    for d in /sys/class/rc/rc*; do
        if grep -qs gpio_ir_recv "$d"/input*/name; then
            exec /usr/bin/ir-keytable -s "$(basename "$d")" -c -p nec -w /etc/rc_keymaps/elegoo-21-keymap
        fi
    done
    sleep 1
done
echo "gpio_ir_recv device not found" >&2
exit 1
