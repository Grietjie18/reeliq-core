name: Daily SST Fetch

on:
  schedule:
    - cron: '0 4 * * *'  # Runs at 4am UTC daily (6am SA time)
  workflow_dispatch:  # Allows manual trigger

jobs:
  fetch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install copernicusmarine numpy shapely

      - name: Fetch SST data
        env:
          COPERNICUS_USERNAME: ${{ secrets.COPERNICUS_USERNAME }}
          COPERNICUS_PASSWORD: ${{ secrets.COPERNICUS_PASSWORD }}
        run: python fetch_satellite.py

      - name: Commit satellite data
        run: |
          git config --global user.name 'github-actions'
          git config --global user.email 'actions@github.com'
          git add satellite_sst.json
          git commit -m "Daily SST update $(date +%Y-%m-%d)" || exit 0
          git push
