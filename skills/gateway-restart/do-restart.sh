#!/bin/bash
# Delayed gateway restart — run as a disowned process.
# The sleep gives the calling session time to finish responding.
sleep 10
exec kirocrew restart
