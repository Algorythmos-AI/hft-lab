"""The host and operations layer: what runs the platform on its server, and what tells the owner how it is.

`host` holds the names the platform owns on a machine it shares; `units` renders the systemd units from code;
`preflight` decides from a day of measurements whether the platform may be installed at all; `status` reads the
recorder's status file and the journals and judges them, dead-man rule included; `alerts` pages once per
problem; `backup` hands the journals to somewhere else; `post` is the one HTTPS request this layer makes.

Nothing here logs in to a broker, holds a credential or enables a unit. The owner runs the scripts in
deploy/bin, and every one of them says what it will do before it does it (ADR 0007, ADR 0017).
"""
