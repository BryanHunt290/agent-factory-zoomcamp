"""Versioned SQL migrations, applied in order by ``agent_factory.persistence.migrator``.

Files are named ``NNNN_description.sql``. An applied migration is never
edited: the migrator records each file's checksum and refuses to run if a
recorded migration has changed. Schema changes are new files.
"""
