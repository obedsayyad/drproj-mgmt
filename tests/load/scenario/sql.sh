#!/bin/bash
docker exec drproj-mgmt-st2actionrunner-1 /opt/stackstorm/virtualenvs/cdr/bin/python /tmp/sql.py "$1"
