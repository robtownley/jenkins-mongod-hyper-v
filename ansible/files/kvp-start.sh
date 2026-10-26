#!/bin/sh
set -eu
udevadm control --reload-rules
udevadm trigger --action=add '/sys/devices/virtual/misc/vmbus!hv_kvp'
udevadm settle
systemctl start hypervkvpd.service
