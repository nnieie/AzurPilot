"""Shared cloud API origin; usable before application dependencies are loaded."""

API_BASE_URL = 'https://api.azurpilot.com'
FORCE_UPDATE_CONTROL_URL = f'{API_BASE_URL}/api/v2/get/update/data.json'
MAINTENANCE_API_URL = f'{API_BASE_URL}/api/v2/get/maintenance'
UPLOAD_API_URL = f'{API_BASE_URL}/api/v1/uploads'
