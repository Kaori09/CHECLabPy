import numpy as np
import pandas as pd
from os.path import dirname, exists
from os import remove
from abc import ABC, abstractmethod
from target_io import TargetIOEventReader as TIOReader
from target_io import T_SAMPLES_PER_WAVEFORM_BLOCK as N_BLOCKSAMPLES
from CHECLabPy.utils.files import create_directory

# CHEC-S
N_ROWS = 8
N_COLUMNS = 16
N_BLOCKS = N_ROWS * N_COLUMNS
N_CELLS = N_ROWS * N_COLUMNS * N_BLOCKSAMPLES
SKIP_SAMPLE = 0
SKIP_END_SAMPLE = 0
SKIP_EVENT = 2
SKIP_END_EVENT = 1


class Reader:
    """
    Reader for the R0 and R1 tio files
    """
    def __init__(self, path, max_events=None):
        self.path = path

        self.reader = TIOReader(self.path, N_CELLS,
                                SKIP_SAMPLE, SKIP_END_SAMPLE,
                                SKIP_EVENT, SKIP_END_EVENT)

        self.is_r1 = self.reader.fR1
        self.n_events = self.reader.fNEvents
        self.run_id = self.reader.fRunID
        self.n_pixels = self.reader.fNPixels
        self.n_modules = self.reader.fNModules
        self.n_tmpix = self.n_pixels // self.n_modules
        self.n_samples = self.reader.fNSamples
        self.n_cells = self.reader.fNCells

        self.first_cell_ids = np.zeros(self.n_pixels, dtype=np.uint16)

        if self.is_r1:
            self.samples = np.zeros((self.n_pixels, self.n_samples),
                                    dtype=np.float32)
            self.get_tio_event = self.reader.GetR1Event
        else:
            self.samples = np.zeros((self.n_pixels, self.n_samples),
                                    dtype=np.uint16)
            self.get_tio_event = self.reader.GetR0Event

        if max_events and max_events < self.n_events:
            self.n_events = max_events

    def __iter__(self):
        for iev in range(self.n_events):
            self.index = iev
            self.get_tio_event(iev, self.samples, self.first_cell_ids)
            yield self.samples

    def __getitem__(self, iev):
        self.index = iev
        self.get_tio_event(iev, self.samples, self.first_cell_ids)
        return np.copy(self.samples)


class ReaderR1(Reader):
    """
    Reader for the R1 tio files
    """
    def __init__(self, path, max_events=None):
        super().__init__(path, max_events)
        if not self.is_r1:
            raise IOError("This script is only setup to read *_r1.tio files!")


class ReaderR0(Reader):
    """
    Reader for the R0 tio files
    """
    def __init__(self, path, max_events=None):
        super().__init__(path, max_events)
        if self.is_r1:
            raise IOError("This script is only setup to read *_r0.tio files!")


class DL1Writer:
    """
    Writer for the HDF5 DL1 Files
    """
    def __init__(self, dl1_path, totalrows, monitor_path=None):
        print("Creating HDF5 file: {}".format(dl1_path))
        create_directory(dirname(dl1_path))
        if exists(dl1_path):
            remove(dl1_path)

        self.totalrows = totalrows
        self.metadata = {}
        self.n_bytes = 0
        self.df_list = []
        self.df_list_n_bytes = 0
        self.monitor = None

        self.store = pd.HDFStore(
            dl1_path, complevel=9, complib='blosc:blosclz'
        )

        if monitor_path:
            self.monitor = MonitorWriter(monitor_path, self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finish()

    def _append_to_file(self):
        if self.df_list:
            df = pd.concat(self.df_list, ignore_index=True)

            default_c = [
                't_event',
                't_pulse',
                'amp_pulse',
                'charge',
                'fwhm',
                'tr'
                'baseline_start_mean',
                'baseline_start_rms',
                'baseline_end_mean',
                'baseline_end_rms',
                'baseline_subtracted',
                'waveform_mean',
                'waveform_rms',
                'saturation_coeff'
            ]
            for column in default_c:
                if column not in df:
                    df[column] = 0.

            df_float = df.select_dtypes(
                include=['float']
            ).apply(pd.to_numeric, downcast='float')
            df[df_float.columns] = df_float
            df['iev'] = df['iev'].astype(np.uint32)
            df['pixel'] = df['pixel'].astype(np.uint32)
            df['first_cell_id'] = df['first_cell_id'].astype(np.uint16)
            df['t_tack'] = df['t_tack'].astype(np.uint64)
            df['t_event'] = df['t_event'].astype(np.uint16)

            df = df.sort_values(["iev", "pixel"])
            self.store.append('data', df, index=False, data_columns=True,
                              expectedrows=self.totalrows)
            self.n_bytes += df.memory_usage(index=True, deep=True).sum()
            self.df_list = []
            self.df_list_n_bytes = 0

    def append_event(self, df_ev):
        self.df_list.append(df_ev)
        self.df_list_n_bytes += df_ev.memory_usage(index=True, deep=True).sum()
        if self.monitor:
            self.monitor.match_to_data_events(df_ev)
        if self.df_list_n_bytes >= 0.5E9:
            self._append_to_file()

    def add_metadata(self, **kwargs):
        self.metadata = dict(**self.metadata, **kwargs)

    def _save_metadata(self):
        print("Saving data metadata to HDF5 file")
        self.store.get_storer('data').attrs.metadata = self.metadata

    def finish(self):
        self._append_to_file()
        # cols = self.store.get_storer('data').attrs.non_index_axes[0][1]
        # self.store.create_table_index('data', columns=['iev'], kind='full')
        if self.monitor:
            self.monitor.finish()
        self.add_metadata(n_bytes=self.n_bytes)
        self._save_metadata()
        self.store.close()


class MonitorWriter:
    """
    Read the monitor ASCII file and create the monitor DataFrame in the
    DL1 file
    """

    def __init__(self, monitor_path, dl1_writer):
        print("WARNING MonitorWriter assumes monitor timestamps are in "
              "MPIK time. This needs fixing once they have been converted "
              "to unix/UTC")

        print("Reading monitor information from: {}".format(monitor_path))
        if not exists(monitor_path):
            FileNotFoundError("Cannot find monitor file: {}"
                              .format(monitor_path))

        self.store = dl1_writer.store

        self.supported = [
            "TM_T_PRI",
            "TM_T_AUX",
            "TM_T_PSU",
            "TM_T_SIPM"
        ]

        self.metadata = {}
        self.n_bytes = 0
        self.df_list = []
        self.df_list_n_bytes = 0

        self.n_modules = 32
        self.n_tmpix = 64
        self.empty_df = pd.DataFrame(dict(
            imon=0,
            t_cpu=pd.to_datetime(0, unit='ns'),
            module=np.arange(self.n_modules),
            **dict.fromkeys(self.supported, np.nan)
        ))
        self.eof = False
        self.aeof = False
        self.t_delta_max = pd.Timedelta(0)

        self.monitor_it = self._get_next_monitor_event(monitor_path)
        try:
            self.monitor_ev = next(self.monitor_it)
        except StopIteration:
            raise IOError("No monitor events found")
        self.next_monitor_ev = self.monitor_ev.copy()

    def _get_next_monitor_event(self, monitor_path):
        imon = 0
        t_cpu = 0
        start_time = 0
        df = self.empty_df.copy()
        with open(monitor_path) as file:
            for line in file:
                if line:
                    try:
                        data = line.replace('\n', '').split(" ")

                        t_cpu = pd.to_datetime(
                            "{} {}".format(data[0], data[1]),
                            format="%Y-%m-%d %H:%M:%S:%f"
                        )
                        # TODO: store monitor ASCII with UTC timestamps
                        t_cpu -= pd.Timedelta(1, unit='h')

                        if 'Monitoring Event Done' in line:
                            if not start_time:
                                start_time = t_cpu
                            df.loc[:, 'imon'] = imon
                            df.loc[:, 't_cpu'] = t_cpu
                            self.append_monitor_event(df)
                            yield df
                            imon += 1
                            df = self.empty_df.copy()
                            continue

                        if len(data) < 6:
                            continue

                        device = data[2]
                        measurement = data[3]
                        key = device + "_" + measurement
                        if key in self.supported:
                            device_id = int(data[4])
                            value = float(data[5])

                            df.loc[device_id, key] = value

                    except ValueError:
                        print("ValueError from line: {}".format(line))

            metadata = dict(
                input_path=monitor_path,
                n_events=imon,
                start_time=start_time,
                end_time=t_cpu,
                n_modules=self.n_modules,
                n_tmpix=self.n_tmpix
            )
            self.add_metadata(**metadata)

    def match_to_data_events(self, data_ev):
        t_cpu_data = data_ev.loc[0, 't_cpu']
        t_cpu_next_monitor = self.next_monitor_ev.loc[0, 't_cpu']
        if self.t_delta_max < t_cpu_next_monitor - t_cpu_data:
            self.t_delta_max = t_cpu_next_monitor - t_cpu_data

        # Get next monitor event until the times match
        while (t_cpu_data > t_cpu_next_monitor) and not self.eof:
            try:
                self.monitor_ev = self.next_monitor_ev.copy()
                self.next_monitor_ev = next(self.monitor_it)
                t_cpu_next_monitor = self.next_monitor_ev.loc[0, 't_cpu']
            except StopIteration:
                self.eof = True
                # Use last monitor event for t_delta_max seconds
                imon = self.monitor_ev.loc[0, 'imon']
                t_cpu = self.monitor_ev.loc[0, 't_cpu']
                self.next_monitor_ev = self.empty_df.copy()
                self.next_monitor_ev.loc[:, 'imon'] = imon + 1
                self.next_monitor_ev.loc[:, 't_cpu'] = t_cpu + self.t_delta_max
                t_cpu_next_monitor = self.next_monitor_ev.loc[0, 't_cpu']
        if self.eof and (t_cpu_data > t_cpu_next_monitor) and not self.aeof:
            # Add empty monitor event to file
            print("WARNING: End of monitor events reached, "
                  "setting new monitor items to NaN")
            self.monitor_ev = self.next_monitor_ev.copy()
            self.append_monitor_event(self.monitor_ev)
            self.aeof = True

        imon = self.monitor_ev.loc[0, 'imon']
        module = data_ev.loc[:, 'pixel'] // self.n_tmpix
        data_ev['monitor_index'] = imon * self.n_modules + module

    def _append_to_file(self):
        if self.df_list:
            df = pd.concat(self.df_list, ignore_index=True)

            df_float = df.select_dtypes(
                include=['float']
            ).apply(pd.to_numeric, downcast='float')
            df[df_float.columns] = df_float
            df['imon'] = df['imon'].astype(np.uint32)
            df['module'] = df['module'].astype(np.uint8)

            self.store.append('monitor', df, index=False, data_columns=True)
            self.n_bytes += df.memory_usage(index=True, deep=True).sum()
            self.df_list = []
            self.df_list_n_bytes = 0

    def append_monitor_event(self, df_ev):
        self.df_list.append(df_ev)
        self.df_list_n_bytes += df_ev.memory_usage(index=True, deep=True).sum()
        if self.df_list_n_bytes >= 0.5E9:
            self._append_to_file()

    def add_metadata(self, **kwargs):
        self.metadata = dict(**self.metadata, **kwargs)

    def _save_metadata(self):
        print("Saving monitor metadata to HDF5 file")
        self.store.get_storer('monitor').attrs.metadata = self.metadata

    def finish(self):
        # Finish processing monitor file
        for _ in self.monitor_it:
            pass
        self._append_to_file()
        self.add_metadata(n_bytes=self.n_bytes)
        self._save_metadata()


class HDFStoreReader(ABC):
    """
    Base class for reading from HDFStores
    """
    @abstractmethod
    def __init__(self):
        self.store = None
        self.key = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.store.close()

    @property
    def metadata(self):
        return self.store.get_storer(self.key).attrs.metadata

    @property
    def columns(self):
        return self.store.get_storer(self.key).attrs.non_index_axes[0][1]

    @property
    def n_rows(self):
        return self.store.get_storer(self.key).nrows

    @property
    def n_bytes(self):
        return self.metadata['n_bytes']

    def load_entire_table(self, force=False):
        """
        Load the entire DataFrame into memory

        Parameters
        ----------
        force : bool
            Force the loading of a DataFrame into memory even if it is of a
            large size

        Returns
        -------
        df : `pandas.DataFrame`

        """
        print("Loading entire DataFrame from HDF5 file")
        if (self.n_bytes > 8E9) and not force:
            raise MemoryError("DataFrame is larger than 8GB, "
                              "set force=True to proceed with loading the "
                              "entire DataFrame into memory")
        if self.n_bytes > 8E9:
            print("WARNING: DataFrame is larger than 8GB")
        return self.store[self.key]

    def select(self, **kwargs):
        """
        Use the pandas.HDFStore.select method to select a subset of the
        DataFrame.

        Parameters
        ----------
        kwargs
            Arguments to pass to pandas.HDFStore.select

        Returns
        -------
        df : `pandas.DataFrame`

        """
        return self.store.select(self.key, **kwargs)

    def select_column(self, column, **kwargs):
        """
        Use the pandas.HDFStore.select_column method to obtain a single
        column

        Parameters
        ----------
        column : str
            The column you wish to obtain
        kwargs
            Arguments to pass to pandas.HDFStore.select

        Returns
        -------
        `pandas.Series`

        """
        return self.store.select_column(self.key, column, **kwargs)

    def select_columns(self, columns, **kwargs):
        """
        Use the pandas.HDFStore.select_column method to obtain a list of
        columns as numpy arrays

        Parameters
        ----------
        columns : list
            A list of the columns you wish to obtain
        kwargs
            Arguments to pass to pandas.HDFStore.select

        Returns
        -------
        values : list
            List of numpy arrays containing the values for all of the columns

        """
        values = []
        for c in columns:
            values.append(self.select_column(c, **kwargs))
        return values

    def iterate_over_rows(self):
        """
        Loop over the each event in the file, therefore avoiding loading the
        entire table into memory.

        Returns
        -------
        row : `pandas.Series`

        """
        for row in self.iterate_over_chunks(1):
            yield row

    def iterate_over_events(self):
        """
        Loop over the each event in the file, therefore avoiding loading the
        entire table into memory.

        Returns
        -------
        df : `pandas.DataFrame`

        """
        for df in self.iterate_over_chunks(self.metadata['n_pixels']):
            yield df

    def iterate_over_chunks(self, chunksize=None):
        """
        Loop over the DataFrame in chunks, therefore avoiding loading the
        entire table into memory. The chunksize is automatically defined to
        be approximately 2GB of memory.

        Parameters
        ----------
        chunksize : int
            Size of the chunk. By default it is set to a number of rows that
            is approximately equal to 2GB of memory.

        Returns
        -------
        df : `pandas.DataFrame`

        """
        if not chunksize:
            chunksize = self.n_rows / self.n_bytes * 2E9
        for df in self.store.select(self.key, chunksize=chunksize):
            yield df


class DL1Reader(HDFStoreReader):
    """
    Reader for the HDF5 DL1 Files
    """
    def __init__(self, path):
        super().__init__()
        print("Opening HDF5 file: {}".format(path))
        self.store = pd.HDFStore(
            path, mode='r', complevel=9, complib='blosc:blosclz'
        )
        self.key = 'data'
        if 'monitor' in self.store:
            self.monitor = MonitorReader(self.store)

    def get_monitor_column(self, monitor_index, column):
        """
        Get a column from the monitor column corresponding to the
        monitor_index of the currect 'data' DataFrame.

        Parameters
        ----------
        monitor_index : ndarray
            The indicis of the monitor rows requested
        column : str
            Column name from the monitor DataFrame

        Returns
        -------

        """
        try:
            column = self.monitor.select_column(column)[monitor_index]
        except AttributeError:
            raise AttributeError("No monitor information was included in "
                                 "the creation of this file")
        return self.monitor.select_column(column)[monitor_index]


class MonitorReader(HDFStoreReader):
    def __init__(self, store):
        super().__init__()
        self.key = 'monitor'
        self.store = store

    def iterate_over_events(self):
        for df in self.iterate_over_chunks(self.metadata['n_modules']):
            yield df