Put the private location configuration JSON you downloaded from your SANDBOX org here as:

    config/worker-config.json

(Synthetic Monitoring → Settings → Private Locations → Add Private Location → download the
config file.) It contains secrets for that private location, so don't commit or share it.

config/overrides.json is merged on top of it each time the stack starts.
