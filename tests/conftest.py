import os

# Tests use the demo fixtures, never a real CSLB database that may exist in data/.
os.environ["LICENSE_DB"] = "/nonexistent/cslb.sqlite"
