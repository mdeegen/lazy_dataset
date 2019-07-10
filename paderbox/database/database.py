"""
The reader is part of the new database concept 2017.

The task of the reader is to take a database JSON and an dataset identifier as
an input and load all meta data for each observation with corresponding
numpy arrays for each time signal (non stacked).

An example ID often stands for utterance ID. In case of speaker mixtures,
it replaces mixture ID. Also, in case of event detection utterance ID is not
very adequate.

The JSON file is specified as follows:

datasets:
    <dataset name 0>
        <unique example id 1> (unique within a dataset)
            audio_path:
                speech_source:
                    <path to speech of speaker 0>
                    <path to speech of speaker 1>
                observation:
                    blue_array: (a list, since there are no missing channels)
                        <path to observation of blue_array and channel 0>
                        <path to observation of blue_array and channel 0>
                        ...
                    red_array: (special case for missing channels)
                        c0: <path to observation of red_array and channel 0>
                        c99: <path to observation of red_array and channel 99>
                        ...
                speech_image:
                    ...
            speaker_id:
                <speaker_id for speaker 0>
                ...
            gender:
                <m/f>
                ...
            ...

Make sure, all keys are natsorted in the JSON file.

Make sure, the names are not redundant and it is clear, which is train, dev and
test set. The names should be as close as possible to the original database
names.

An observation/ example has information according to the keys file.

If a database does not have different arrays, the array dimension can be
omitted. Same holds true for the channel axis or the speaker axis.

The different axis have to be natsorted, when they are converted to numpy
arrays. Skipping numbers (i.e. c0, c99) is database specific and is not handled
by a generic implementation.

If audio paths are a list, they will be stacked to a numpy array. If it is a
dictionary, it will become a dictionary of numpy arrays.

If the example IDs are not unique in the original database, the example IDs
are made unique by prefixing them with the dataset name of the original
database, i.e. dt_simu_c0123.
"""
import glob
import logging
import operator
import os
from collections import defaultdict
from pathlib import Path
import weakref
from cached_property import cached_property

import numpy as np

import lazy_dataset

from paderbox import kaldi
from paderbox.io import load_json
from paderbox.io.audioread import load_audio

from paderbox.database.keys import *

LOG = logging.getLogger('Database')


class MalformedDatasetError(Exception):
    pass


def to_list(x, item_type=None):
    """
    Note:
        It is recommended to use item_type, when the type of the list is known
        to catch as much cases as possible.
        The problem is that many python functions return a type that does not
        inherit from tuple and/or list.
        e.g. dict keys, dict values, map, sorted, ...

        The instance check with collections.Sequence could produce problem with
        str. (isinstance('any str', collections.Sequence) is True)

    >>> to_list(1)
    [1]
    >>> to_list([1])
    [1]
    >>> to_list((1,))
    (1,)
    >>> to_list({1: 2}.keys())  # Wrong
    [dict_keys([1])]
    >>> to_list({1: 2}.keys(), item_type=int)
    [1]
    """
    if item_type is None:
        if isinstance(x, (list, tuple)):
            return x
        return [x]
    else:
        if isinstance(x, item_type):
            return [x]
        return list(x)


class Database:
    """Base class for databases.

    This class is abstract!"""
    @property
    def database_dict(self):
        raise NotImplementedError(
            f'Override this property in {self.__class__.__name__}!')

    @property
    def dataset_names(self):
        return tuple(
            self.database_dict[DATASETS].keys()
        ) + tuple(
            self.database_dict.get(ALIAS, {}).keys()
        )

    @property
    def datasets_train(self):
        """A list of filelist names for training."""
        raise NotImplementedError

    @property
    def datasets_eval(self):
        """A list of filelist names for evaluation."""
        raise NotImplementedError

    @property
    def datasets_test(self):
        """A list of filelist names for testing."""
        raise NotImplementedError

    @cached_property
    def datasets(self):
        """Allows creation of iterator with point notation."""
        return type(
            'DatasetsCollection',
            (object,),
            {
                '__getitem__': (
                    lambda _, dataset_name:
                    self.get_iterator_by_names(dataset_name)
                ),
                'keys': (lambda _: self.dataset_names),
                **{
                    # Create a new property method for each key, which
                    # resolves to `inner_self[dataset_name]`.
                    dataset_name: property(operator.itemgetter(dataset_name))
                    for dataset_name in self.dataset_names
                }
            }
        )()

    def _get_dataset_from_database_dict(self, dataset_name):
        if dataset_name in self.database_dict.get('alias', []):
            dataset_names = self.database_dict['alias'][dataset_name]
            examples = {}
            for name in dataset_names:
                examples_new = self.database_dict[DATASETS][name]
                intersection = set.intersection(
                    set(examples.keys()),
                    set(examples_new.keys()),
                )
                assert len(intersection) == 0, intersection
                examples = {**examples, **examples_new}
            return examples
        else:
            return self.database_dict[DATASETS][dataset_name]

    @cached_property
    def _dataset_weak_ref_dict(self):
        return weakref.WeakValueDictionary()

    def get_dataset(self, names=None):
        """Return a single lazy dataset over specified datasets.

        Adds the example_id and dataset_name to each example dict.

        :param names: list or str specifying the datasets of interest.
            If None an iterator over the complete databases will be returned.
        :return:
        """
        if names is None:
            raise TypeError(
                f'Missing dataset_names, use e.g.: {self.dataset_names}'
            )

        names = to_list(names, item_type=str)
        datasets = list()
        for name in names:
            # Resulting dataset is immutable anyway due to pickle a few lines
            # further down. This code here avoids to store the resulting
            # dataset more than once in memory. Discuss with CBJ for details.
            try:
                ds = self._dataset_weak_ref_dict[name]
            except KeyError:
                pass
            else:
                datasets.append(ds)
                continue

            try:
                examples = self._get_dataset_from_database_dict(name)
            except KeyError:
                import difflib
                similar = difflib.get_close_matches(
                    name,
                    self.dataset_names,
                    n=5,
                    cutoff=0,
                )
                raise KeyError(name, f'close_matches: {similar}', self)
            if len(examples) == 0:
                # When somebody need empty datasets, add an option to this
                # function to allow empty datasets.
                raise RuntimeError(
                    f'The requested dataset {name!r} is empty. '
                )

            for example_id in examples.keys():
                examples[example_id][EXAMPLE_ID] = example_id
                examples[example_id][DATASET_NAME] = name

            ds = lazy_dataset.from_dict(examples)

            self._dataset_weak_ref_dict[name] = ds

            datasets.append(ds)

        return lazy_dataset.concatenate(*datasets)

    def get_iterator_by_names(self, dataset_names=None):
        """Alias of get_dataset_by_names.

        Iterators are lazy datasets now.
        This provides compatibility with the way things used to be.
        """
        return self.get_dataset(dataset_names)

    def get_bucket_boundaries(
            self, datasets, num_buckets=1, length_transform_fn=lambda x: x
    ):
        try:
            lengths = self.get_lengths(datasets, length_transform_fn)
            lengths_list = [length for length in lengths.values()]
            percentiles = np.linspace(
                0, 100, num_buckets + 1, endpoint=True)[1:-1]
            return np.percentile(lengths_list, percentiles,
                                 interpolation='higher')
        except NotImplementedError:
            assert num_buckets == 1, num_buckets
            return []

    @property
    def read_fn(self):
        return lambda x: load_audio(x)

    def get_lengths(self, datasets, length_transform_fn=lambda x: x):
        it = self.get_iterator_by_names(datasets)
        lengths = dict()
        for example in it:
            num_samples = example[NUM_SAMPLES]
            if isinstance(num_samples, dict):
                num_samples = num_samples[OBSERVATION]
            example_id = example[EXAMPLE_ID]
            lengths[example_id] = (length_transform_fn(num_samples))
        return lengths

    def add_num_samples(self, example):
        if NUM_SAMPLES in example:
            return example
        else:
            raise NotImplementedError


class DictDatabase(Database):
    def __init__(self, database_dict: dict):
        """A simple database class intended to hold a given database_dict.

        :param database_dict: A json serializeable database dictionary.
        """
        self._database_dict = database_dict
        super().__init__()

    @property
    def database_dict(self):
        return self._database_dict


class JsonDatabase(Database):
    def __init__(self, json_path: [str, Path]):
        """

        :param json_path: path to database JSON
        """
        self._json_path = json_path
        super().__init__()

    @cached_property
    def database_dict(self):
        LOG.info(f'Using json {self._json_path}')
        return load_json(self._json_path)

    def __repr__(self):
        return f'{type(self).__name__}({self._json_path!r})'


class KaldiDatabase(Database):
    """A database representing information from a kaldi recipe directory.

    Which files are expected from the egs directory to be a Kaldi database?
    - data
        - filst1
            - wav.scp with format: <utterance_id> <audio_path>
            - utt2spk with format: <utterance_id> <speaker_id>
            - text with format: <utterance_id> <kaldi_word_transcription>
            - spk2gender (optional)
            - utt2dur (optional, useful for correct bucketing)
        - flist2
            - wav.scp
            - utt2spk
            - text
            - spk2gender (optional)
            - utt2dur (optional, useful for correct bucketing)

    The `wav.scp` should ideally be in this format:
        utt_id1 audio_path1
        utt_id2 audio_path2
    """
    def __init__(self, egs_path: Path):
        self._egs_path = Path(egs_path)
        super().__init__()

    def __repr__(self):
        return f'{type(self).__name__}: {self._egs_path}'

    @cached_property
    def database_dict(self):
        LOG.info(f'Using kaldi recipe at {self._egs_path}')
        # Wrong value of e.g. $KALDI_ROOT may result in improper egs path.
        assert os.path.isdir(self._egs_path), \
            f'egs path not set properly! Current value: {self._egs_path}'
        database_dict = self.get_dataset_dict_from_kaldi(self._egs_path)
        return self._add_num_samples_to_database_dict(database_dict)

    @classmethod
    def length_transform_fn(cls, length):
        raise NotImplementedError(
            'Implement a `length_transform_fn` which translates from '
            'seconds (due to Kaldi) to your desired lengths. '
            'It can not be implemented here '
            'since the sample rate is not known.'
        )


    @staticmethod
    def get_examples_from_dataset(dataset_path):
        dataset_path = Path(dataset_path)
        scp = kaldi.io.load_keyed_lines(dataset_path / 'wav.scp', to_list=True)
        utt2spk = kaldi.io.load_keyed_lines(dataset_path / 'utt2spk')
        text = kaldi.io.load_keyed_lines(dataset_path / 'text')
        try:
            spk2gender = kaldi.io.load_keyed_lines(
                dataset_path / 'spk2gender'
            )
        except FileNotFoundError:
            spk2gender = None
        examples = dict()

        # Normally the scp points to a single audio file (i.e. len(s) = 1)
        # For databases with a different audio format (e.g. WSJ) however,
        # it is a command to convert the corresponding audio file. The
        # file is usually at the end of this command. If this does not work,
        # additional heuristics need to be introduced here.
        def _audio_path(s):
            if len(s) == 1:
                return s[0]
            else:
                return s[-2]

        for example_id in scp.keys():
            example = defaultdict(dict)
            example[AUDIO_PATH][OBSERVATION] = _audio_path(scp[example_id])
            try:
                example[SPEAKER_ID] = utt2spk[example_id]
            except KeyError as e:
                raise MalformedDatasetError(
                    f'Example id {example_id} not found in utt2spk.\n'
                    f'Skipping dataset at path {dataset_path}.')
            if spk2gender is not None:
                example[GENDER] = spk2gender[example[SPEAKER_ID]]
            example[KALDI_TRANSCRIPTION] = text[example_id]
            examples[example_id] = dict(**example)
        return examples

    def get_lengths(self, datasets, length_transform_fn=lambda x: x):
        if not isinstance(datasets, (tuple, list)):
            datasets = [datasets]
        lengths = dict()
        for dataset in datasets:
            try:
                # assume that num_samples are present
                dataset_lengths = {
                    k: length_transform_fn(v[NUM_SAMPLES])
                    for k, v in self.database_dict[DATASETS][dataset].items()}
            except (KeyError, AttributeError):
                raise NotImplementedError(
                    'No length information present. '
                    'Map with add_num_samples method instead.')
            lengths.update(dataset_lengths)
        return lengths

    def add_num_samples(self, example):
        assert (
            AUDIO_DATA in example
            and OBSERVATION in example[AUDIO_DATA]
        ), (
            'No audio data found in example. Make sure to map with '
            '`AudioReader` before adding `num_samples`.'
        )
        example[NUM_SAMPLES] = example[AUDIO_DATA][OBSERVATION].shape[-1]
        return example

    @classmethod
    def get_dataset_dict_from_kaldi(cls, egs_path):
        egs_path = Path(egs_path)
        scp_paths = glob.glob(str(egs_path / 'data' / '*' / 'wav.scp'))
        dataset_dict = {DATASETS: {}}
        for wav_scp_file in scp_paths:
            dataset_path = Path(wav_scp_file).parent
            dataset_name = dataset_path.name
            try:
                examples = cls.get_examples_from_dataset(dataset_path)
            except MalformedDatasetError as e:
                LOG.warning(' '.join(e.args))
                continue
            dataset_dict[DATASETS][dataset_name] = examples
        return dataset_dict

    def _add_num_samples_to_database_dict(self, database_dict):
        """Add number of samples directly to the database_dict.

        This is useful when one wants to write the database_dict to a json
        file in order to instantiate a JsonDatabase from it later.
        The calculation of the number of samples uses the utt2dur file
        from the kaldi recipe and the default length_transfom_fn of the
        database which should be based on the sample rate.
        """
        dataset_names = list(database_dict[DATASETS].keys())
        try:
            num_samples_dict = self._get_lengths_from_kaldi(database_dict)
        except NotImplementedError as e:
            LOG.warning('num_samples information not added to database_dict.\n'
                        ' This is caused by the following exception:\n'
                        ' '.join(e.args))
            return database_dict
        for name in dataset_names:
            for key in database_dict[DATASETS][name].keys():
                database_dict[DATASETS][name][key][NUM_SAMPLES] = \
                    num_samples_dict[key]
        return database_dict

    def _get_lengths_from_kaldi(self, database_dict):
        lengths = dict()
        for dataset in database_dict[DATASETS].keys():
            # read out utt2dur file for lengths
            utt2dur_path = self._egs_path / 'data' / dataset / 'utt2dur'
            if not utt2dur_path.is_file():
                raise NotImplementedError(
                    'Lengths only available for bucketing if utt2dur file '
                    f'exists: {utt2dur_path}'
                )
            dataset_lengths = {
                k: self.length_transform_fn(float(v))
                for k, v in kaldi.io.load_keyed_lines(utt2dur_path).items()}
            lengths.update(dataset_lengths)
        return lengths


class HybridASRDatabaseTemplate:

    def __init__(self, lfr=False):
        self.lfr = lfr

    @property
    def ali_path_train(self):
        """Path containing the kaldi alignments for train data."""
        if self.lfr:
            return self.ali_path_train_lfr
        else:
            return self.ali_path_train_ffr

    @property
    def ali_path_train_ffr(self):
        """Path containing the kaldi alignments for train data."""
        raise NotImplementedError

    @property
    def ali_path_train_lfr(self):
        """Path containing the kaldi alignments for train data."""
        raise NotImplementedError

    @property
    def ali_path_eval(self):
        """Path containing the kaldi alignments for dev data."""
        if self.lfr:
            return self.ali_path_eval_lfr
        else:
            return self.ali_path_eval_ffr

    @property
    def ali_path_eval_ffr(self):
        """Path containing the kaldi alignments for dev data."""
        raise NotImplementedError

    @property
    def ali_path_eval_lfr(self):
        """Path containing the kaldi alignments for dev data."""
        raise NotImplementedError

    @property
    def hclg_path(self):
        """Path to HCLG directory created by Kaldi."""
        if self.lfr:
            return self.hclg_path_lfr
        else:
            return self.hclg_path_ffr

    @property
    def egs_path(self):
        raise NotImplementedError

    @property
    def lang_path(self):
        return self.egs_path / 'data' / 'lang'

    @property
    def hclg_path_ffr(self):
        """Path to HCLG directory created by Kaldi."""
        raise NotImplementedError

    @property
    def hclg_path_lfr(self):
        """Path to HCLG directory created by Kaldi."""
        return self.ali_path_train_lfr / 'graph_tgpr_5k'

    @property
    def example_id_map_fn(self):
        return lambda x: x[EXAMPLE_ID]

    @property
    def decode_fst(self):
        """A string pointing to HCLG.fst from the kaldi recipe."""
        return str(self.hclg_path / 'HCLG.fst')

    @property
    def words_txt(self):
        """A string pointing to the `words.txt` created by Kaldi."""
        return str(self.hclg_path / 'words.txt')

    @property
    def model_file(self):
        return str(self.ali_path_train / 'final.mdl')

    @property
    def tree_file(self):
        return str(self.ali_path_train / 'tree')

    @property
    def phones(self):
        return str(self.ali_path_train / 'phones.txt')

    @property
    def occs_file(self):
        if self.lfr:
            return self.occs_file_lfr
        else:
            return self.occs_file_ffr

    @property
    def occs_file_ffr(self):
        return str(self.ali_path_train_ffr / 'final.occs')

    @property
    def occs_file_lfr(self):
        return str(self.ali_path_train_lfr / '1.occs')

    @cached_property
    def occs(self):
        """An array with the number of occurances for each state."""
        return kaldi.alignment.import_occs(self.occs_file)

    @cached_property
    def _id2word_dict(self):
        return kaldi.io.id2word(self.words_txt)

    @cached_property
    def _word2id_dict(self):
        return kaldi.io.word2id(self.words_txt)

    @cached_property
    def _phone2id_dict(self):
        return kaldi.io.keyed_lines.load_keyed_lines(self.phones)

    @cached_property
    def _id2phone_dict(self):
        # `v` was indexed with 0. Was this intentional? Did not work for Chime3
        # so I removed the index
        return {int(v): k for k, v in self._phone2id_dict.items()}

    def phone2id(self, phone):
        return self._phone2id_dict[phone]

    def id2phone(self, id_):
        return self._id2phone_dict[id_]

    def word2id(self, word):
        """Returns the integer ID for a given word.

        If the word is not found, it returns the ID for `<UNK>`.
        """
        try:
            return self._word2id_dict[word]
        except KeyError:
            return self._word2id_dict['<UNK>']

    def id2word(self, _id):
        """Returns the word corresponding to `_id`."""
        return self._id2word_dict[_id]

    def get_length_for_dataset(self, dataset):
        return len(self.get_iterator_by_names(dataset))

    def write_text_file(self, filename, datasets):
        iterator = self.get_iterator_by_names(datasets)
        with open(filename, 'w') as fid:
            for example in iterator:
                fid.write(
                    f'{example[EXAMPLE_ID]} '
                    f'{example[KALDI_TRANSCRIPTION]}\n'
                )

    def utterances_for_dataset(self, dataset):
        iterator = self.get_iterator_by_names(dataset)
        return [ex[EXAMPLE_ID] for ex in iterator]

    @cached_property
    def state_alignment(self):
        alignments = kaldi.alignment.import_alignment_data(
            self.ali_path_eval, model_name=self.model_file
        )
        alignments.update(kaldi.alignment.import_alignment_data(
            self.ali_path_train, model_name=self.model_file
        ))
        return alignments

    @cached_property
    def phone_alignment(self):
        alignments = kaldi.alignment.import_alignment_data(
            self.ali_path_train,
            import_fn=kaldi.alignment.import_phone_alignment_from_file,
            per_frame=True, model_name=self.model_file
        )
        alignments.update(kaldi.alignment.import_alignment_data(
            self.ali_path_eval,
            import_fn=kaldi.alignment.import_phone_alignment_from_file,
            per_frame=True, model_name=self.model_file
        ))
        return alignments

    @cached_property
    def vad(self):
        alignment = self.phone_alignment
        with open(self.lang_path / 'phones' / 'silence.csl') as fid:
            silence_ids = list(map(int, fid.read().strip().split(':')))
        return {
            k: np.asarray([int(_id) not in silence_ids for _id in v])
            for k, v in alignment.items()
        }

    @property
    def asr_observation_key(self):
        return OBSERVATION

    def build_select_channels_map_fn(self, channels):
        def select_channels(example):
            assert channels == [0], (
                f'Requested to select channels {channels}, but the '
                f'database is only single-channel. Please only request '
                f'channel 0 in this case (channels = [0]).'
            )
            return example
        return select_channels

    def build_sample_channels_map_fn(self, channels):
        def sample_channels(example):
            assert channels == [0], (
                f'Requested to sample from channels {channels}, but the '
                f'database is only single-channel. Please only request '
                f'channel 0 in this case (channels = [0]).'
            )
            return example
        return sample_channels


class HybridASRJSONDatabaseTemplate(HybridASRDatabaseTemplate, JsonDatabase):
    def __init__(self, json_path: Path, lfr=False):
        super().__init__(lfr=lfr)
        super(HybridASRDatabaseTemplate, self).__init__(json_path=json_path)


class HybridASRKaldiDatabaseTemplate(HybridASRDatabaseTemplate, KaldiDatabase):
    def __init__(self, egs_path: Path, lfr=False):
        super().__init__(lfr=lfr)
        super(HybridASRDatabaseTemplate, self).__init__(egs_path=egs_path)
