#!/bin/bash
set -o errexit

conda create -n sandbox-runtime -y python=3.10

source activate sandbox-runtime

pip install -r ./requirements.txt --ignore-requires-python

pip cache purge
conda clean --all -y
