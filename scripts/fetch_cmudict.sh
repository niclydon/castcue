#!/bin/sh
# Fetch the CMU Pronouncing Dictionary next to server.py.
# Source: Carnegie Mellon University cmudict (free redistribution
# with attribution). CastCue fetches it rather than bundling it.
set -e
cd "$(dirname "$0")/.."
curl -L -o cmudict.txt https://raw.githubusercontent.com/cmusphinx/cmudict/master/cmudict.dict
echo "cmudict.txt: $(wc -l < cmudict.txt) entries"
