"""Provide file loading and saving functionality for Ibis's backends."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar

import ibis.expr.types as ir
from kedro.io import AbstractVersionedDataset, DatasetError, Version

if TYPE_CHECKING:
    from ibis import BaseBackend


class FileDataset(AbstractVersionedDataset[ir.Table, ir.Table]):
    """``FileDataset`` loads/saves data from/to a specified file format.

    Example usage for the
    `YAML API <https://docs.kedro.org/en/stable/data/data_catalog_yaml_examples.html>`_:

    .. code-block:: yaml

        cars:
          type: ibis.FileDataset
          filepath: data/01_raw/company/cars.csv
          file_format: csv
          table_name: cars
          connection:
            backend: duckdb
            database: company.db
          load_args:
            sep: ","
            nullstr: "#NA"
          save_args:
            sep: ","
            nullstr: "#NA"

        motorbikes:
          type: ibis.FileDataset
          filepath: s3://your_bucket/data/02_intermediate/company/motorbikes/
          file_format: delta
          table_name: motorbikes
          connection:
            backend: polars

    Example usage for the
    `Python API <https://docs.kedro.org/en/stable/data/\
    advanced_data_catalog_usage.html>`_:

    .. code-block:: pycon

        >>> import ibis
        >>> from kedro_datasets.ibis import FileDataset
        >>>
        >>> data = ibis.memtable({"col1": [1, 2], "col2": [4, 5], "col3": [5, 6]})
        >>>
        >>> dataset = FileDataset(
        ...     filepath=tmp_path / "test.csv",
        ...     file_format="csv",
        ...     table_name="test",
        ...     connection={"backend": "duckdb", "database": tmp_path / "file.db"},
        ... )
        >>> dataset.save(data)
        >>> reloaded = dataset.load()
        >>> assert data.execute().equals(reloaded.execute())

    """

    DEFAULT_CONNECTION_CONFIG: ClassVar[dict[str, Any]] = {
        "backend": "duckdb",
        "database": ":memory:",
    }
    DEFAULT_LOAD_ARGS: ClassVar[dict[str, Any]] = {}
    DEFAULT_SAVE_ARGS: ClassVar[dict[str, Any]] = {}

    _connections: ClassVar[dict[tuple[tuple[str, str]], BaseBackend]] = {}

    def __init__(  # noqa: PLR0913
        self,
        filepath: str,
        file_format: str = "parquet",
        *,
        table_name: str | None = None,
        connection: dict[str, Any] | None = None,
        load_args: dict[str, Any] | None = None,
        save_args: dict[str, Any] | None = None,
        version: Version | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Creates a new ``FileDataset`` pointing to the given filepath.

        ``FileDataset`` connects to the Ibis backend object constructed
        from the connection configuration. The `backend` key provided in
        the config can be any of the `supported backends <https://ibis-\
        project.org/install>`_. The remaining dictionary entries will be
        passed as arguments to the underlying ``connect()`` method (e.g.
        `ibis.duckdb.connect() <https://ibis-project.org/backends/duckdb\
        #ibis.duckdb.connect>`_).

        The read method corresponding to the given ``file_format`` (e.g.
        `read_csv() <https://ibis-project.org/backends/\
        duckdb#ibis.backends.duckdb.Backend.read_csv>`_) is used to load
        the file with the backend. Note that only the data is loaded; no
        link to the underlying file exists past ``FileDataset.load()``.

        Args:
            filepath: Path to a file to register as a table. Most useful
                for loading data into your data warehouse (for testing).
                On save, the backend exports data to the specified path.
            file_format: String specifying the file format for the file.
                Defaults to writing execution results to a Parquet file.
            table_name: The name to use for the created table (on load).
            connection: Configuration for connecting to an Ibis backend.
                If not provided, connect to DuckDB in in-memory mode.
            load_args: Additional arguments passed to the Ibis backend's
                `read_{file_format}` method.
            save_args: Additional arguments passed to the Ibis backend's
                `to_{file_format}` method.
            version: If specified, should be an instance of
                ``kedro.io.core.Version``. If its ``load`` attribute is
                None, the latest version will be loaded. If its ``save``
                attribute is None, save version will be autogenerated.
            metadata: Any arbitrary metadata. This is ignored by Kedro,
                but may be consumed by users or external plugins.
        """
        self._file_format = file_format
        self._table_name = table_name
        self._connection_config = connection or self.DEFAULT_CONNECTION_CONFIG
        self.metadata = metadata

        super().__init__(
            filepath=PurePosixPath(filepath),
            version=version,
            exists_function=lambda filepath: Path(filepath).exists(),
        )

        # Set load and save arguments, overwriting defaults if provided.
        self._load_args = deepcopy(self.DEFAULT_LOAD_ARGS)
        if load_args is not None:
            self._load_args.update(load_args)

        self._save_args = deepcopy(self.DEFAULT_SAVE_ARGS)
        if save_args is not None:
            self._save_args.update(save_args)

    @property
    def connection(self) -> BaseBackend:
        """The ``Backend`` instance for the connection configuration."""

        def hashable(value):
            """Return a hashable key for a potentially-nested object."""
            if isinstance(value, dict):
                return tuple((k, hashable(v)) for k, v in sorted(value.items()))
            if isinstance(value, list):
                return tuple(hashable(x) for x in value)
            return value

        cls = type(self)
        key = hashable(self._connection_config)
        if key not in cls._connections:
            import ibis

            config = deepcopy(self._connection_config)
            backend = getattr(ibis, config.pop("backend"))
            cls._connections[key] = backend.connect(**config)

        return cls._connections[key]

    def load(self) -> ir.Table:
        load_path = self._get_load_path()
        reader = getattr(self.connection, f"read_{self._file_format}")
        return reader(load_path, self._table_name, **self._load_args)

    def save(self, data: ir.Table) -> None:
        save_path = self._get_save_path()
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        writer = getattr(self.connection, f"to_{self._file_format}")
        writer(data, save_path, **self._save_args)

    def _describe(self) -> dict[str, Any]:
        return {
            "filepath": self._filepath,
            "file_format": self._file_format,
            "table_name": self._table_name,
            "backend": self._connection_config["backend"],
            "load_args": self._load_args,
            "save_args": self._save_args,
            "version": self._version,
        }

    def _exists(self) -> bool:
        try:
            load_path = self._get_load_path()
        except DatasetError:
            return False

        return Path(load_path).exists()
