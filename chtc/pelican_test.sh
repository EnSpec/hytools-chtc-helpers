#!/bin/bash
# Writes one small file; HTCondor transfers it to the lab server via pelican://.
echo "pelican test cluster $1 from $(hostname) at $(date -u +%FT%TZ)" > "pelican_test_$1.txt"
cat "pelican_test_$1.txt"
