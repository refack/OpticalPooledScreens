#!/usr/bin/env bash

# Look for installation instructions in README.md

source venv/bin/activate
pip install -r requirements.txt
# link ops package instead of copying
# jupyter and snakemake will import code from .py files in the ops/ directory
pip install -e .

