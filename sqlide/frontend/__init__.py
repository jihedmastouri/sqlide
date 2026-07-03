"""Frontend: all GTK4/libadwaita GUI code. Talks to the backend only
through sqlide.backend.db.base.Connector and sqlide.backend.connections,
and always via a worker thread (see util.run_async)."""
