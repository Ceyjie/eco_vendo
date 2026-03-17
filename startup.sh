#!/bin/bash

echo "=== ECO Vendo Startup ==="
date

# Wait for system to fully boot
echo "Waiting for system to settle..."
sleep 10

# Check if GPIOs are accessible
echo "Checking GPIOs..."
for pin in 0 1 3 6 10 13 14 21 67 71 110; do
    if [ ! -d "/sys/class/gpio/gpio$pin" ]; then
        echo "Exporting GPIO $pin"
        echo $pin > /sys/class/gpio/export 2>/dev/null || true
    fi
done

# Set servo pin to known state
echo "Initializing servo pin..."
if [ -d "/sys/class/gpio/gpio10" ]; then
    echo "out" > /sys/class/gpio/gpio10/direction
    echo "0" > /sys/class/gpio/gpio10/value
fi

# Start loadcell first
echo "Starting loadcell..."
sudo systemctl restart eco-loadcell.service
sleep 3

# Check if loadcell is running
if systemctl is-active --quiet eco-loadcell.service; then
    echo "Loadcell is running"
else
    echo "WARNING: Loadcell failed to start"
fi

# Start main service
echo "Starting main service..."
sudo systemctl restart eco-main.service

echo "Startup complete"
