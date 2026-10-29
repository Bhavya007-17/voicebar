"""Local voice command bar for Windows."""

import os

# Set these before optional model libraries cache their environment flags.
# Setup may still download explicitly; runtime separately enables offline mode.
for _key in ("HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_UPDATE_CHECK",
             "DISABLE_TELEMETRY", "DO_NOT_TRACK"):
    os.environ[_key] = "1"
del _key
