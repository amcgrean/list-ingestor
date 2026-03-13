@echo off
echo Rebuilding Master & Branch SKU Catalogs...
python sku_cleaner.py
echo.
echo Process complete. The master catalog (erp_items_ai_ready.csv) has been updated.
echo Branch-specific CSVs have also been generated.
pause
