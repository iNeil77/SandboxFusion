#!/bin/bash
# =============================================================================
# install-miniconda.sh -- Download and install Miniconda for a given Python version
# =============================================================================
# Usage: bash install-miniconda.sh <python_version>
#   e.g. bash install-miniconda.sh 3.11
#
# Downloads the Miniconda installer from repo.anaconda.com for the specified
# Python version and installs it non-interactively to /root/miniconda3.
# Python 3.7 is special-cased to use an older Miniconda release (23.1.0-1)
# because newer installers dropped support for it.
# =============================================================================
set -o errexit

# Build the installer filename from the Python version argument ($1).
# The version dots are stripped (e.g. 3.11 -> 311) for the filename convention.
py_ver="$1"
FILENAME=Miniconda3-py${py_ver//.}_23.5.2-0-Linux-x86_64.sh

# Python 3.7 requires an older Miniconda release that still supports it.
if [ "$1" = "3.7" ]
then
FILENAME=Miniconda3-py${py_ver//.}_23.1.0-1-Linux-x86_64.sh
fi

MIRROR="https://repo.anaconda.com/miniconda"

# Download, install in batch mode (-b), and clean up the installer.
wget ${MIRROR}/${FILENAME}
mkdir -p /root/.conda
bash ${FILENAME} -b
rm -f ${FILENAME}
