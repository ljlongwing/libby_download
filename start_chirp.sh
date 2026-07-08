#!/bin/bash
# start_chirp.sh

# Ensure we are in the script's directory
cd "$(dirname "$0")"

# Check if python3 is installed
if ! command -v python3 &> /dev/null
then
    echo "Python 3 could not be found. Please install it."
    exit
fi

# Install requirements if needed
echo "Checking requirements..."
pip install -r requirements.txt --quiet

# Check if playwright is installed
if ! python3 -c "import playwright" &> /dev/null
then
    echo "Installing playwright..."
    pip install playwright
    playwright install chromium
fi

# Run the downloader
python3 chirp_dl.py --out "./MyBooks" "$@"
