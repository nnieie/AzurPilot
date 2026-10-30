"""Shared cloud API origin; usable before application dependencies are loaded."""

API_BASE_URL = 'https://api.azurpilot.com'
FORCE_UPDATE_CONTROL_URL = f'{API_BASE_URL}/api/v2/get/update/data.json'
MAINTENANCE_API_URL = f'{API_BASE_URL}/api/v2/get/maintenance'
DROP_API_URL = f'{API_BASE_URL}/api/v3/post/drops'
TEMPLATE_API_BASE_URL = 'https://templates-api.azurpilot.com'
TEMPLATE_CDN_BASE_URL = 'https://templates.azurpilot.com'
TEMPLATE_PUBLIC_KEY = 'r1HA3vwbOo1v4cDtrU5yJNAKZXuvmW8ot6cXeBC9IN4='
