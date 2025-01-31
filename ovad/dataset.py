from typing import Callable, Dict, List, Union

import torch

from lhotse import validate
from lhotse.cut import CutSet
from lhotse.dataset.input_strategies import InputStrategy, PrecomputedFeatures
from lhotse.utils import ifnone


class K2VadDataset(torch.utils.data.Dataset):
    """
    PyTorch Dataset for the voice activity detection task using K2 library.

    This dataset expects to be queried with lists of cut IDs,
    for which it loads features and automatically collates/batches them.

    To use it with a PyTorch DataLoader, set ``batch_size=None``
    and provide a :class:`SingleCutSampler` sampler.

    Each item in this dataset is a dict of:

    .. code-block::

        {
            'inputs': float tensor with shape determined by :attr:`input_strategy`:
                      - single-channel:
                        - features: (B, T, F)
                        - audio: (B, T)
                      - multi-channel: currently not supported
            'supervisions': [
                {
                    'sequence_idx': Tensor[int] of shape (S,)
                    'text': List[str] of len S

                    # For feature input strategies
                    'start_frame': Tensor[int] of shape (S,)
                    'num_frames': Tensor[int] of shape (S,)

                    # For audio input strategies
                    'start_sample': Tensor[int] of shape (S,)
                    'num_samples': Tensor[int] of shape (S,)

                    # Optionally, when return_cuts=True
                    'cut': List[AnyCut] of len S
                }
            ]
        }

    Dimension symbols legend:
    * ``B`` - batch size (number of Cuts)
    * ``S`` - number of supervision segments (greater or equal to B, as each Cut may have multiple supervisions)
    * ``T`` - number of frames of the longest Cut
    * ``F`` - number of features

    The 'sequence_idx' field is the index of the Cut used to create the example in the Dataset.
    """

    def __init__(
        self,
        cuts: CutSet,
        return_cuts: bool = False,
        cut_transforms: List[Callable[[CutSet], CutSet]] = None,
        input_transforms: List[Callable[[torch.Tensor], torch.Tensor]] = None,
        input_strategy: InputStrategy = PrecomputedFeatures(),
    ):
        """
        K2 VAD IterableDataset constructor.

        :param cuts: the ``CutSet`` to sample data from.
        :param return_cuts: When ``True``, will additionally return a "cut" field in each batch with the Cut
            objects used to create that batch.
        :param cut_transforms: A list of transforms to be applied on each sampled batch,
            before converting cuts to an input representation (audio/features).
            Examples: cut concatenation, noise cuts mixing, etc.
        :param input_transforms: A list of transforms to be applied on each sampled batch,
            after the cuts are converted to audio/features.
            Examples: normalization, SpecAugment, etc.
        :param input_strategy: Converts cuts into a collated batch of audio/features.
            By default, reads pre-computed features from disk.
        """
        super().__init__()
        # Initialize the fields
        self.cuts = cuts
        self.return_cuts = return_cuts
        self.cut_transforms = ifnone(cut_transforms, [])
        self.input_transforms = ifnone(input_transforms, [])
        self.input_strategy = input_strategy
        self._validate()

    def __len__(self) -> int:
        return len(self.cuts)

    def __getitem__(
        self, cut_ids: List[str]
    ) -> Dict[str, Union[torch.Tensor, List[str]]]:
        """
        Return a new batch, with the batch size automatically determined using the contraints
        of max_frames and max_cuts.
        """
        # Collect the cuts that will form a batch, satisfying the criteria of max_cuts and max_frames.
        # The returned object is a CutSet that we can keep on modifying (e.g. padding, mixing, etc.)
        cuts = self.cuts.subset(cut_ids=cut_ids)
        # Sort the cuts by duration so that the first one determines the batch time dimensions.
        cuts = cuts.sort_by_duration(ascending=False)
        # Optional CutSet transforms - e.g. padding, or speed perturbation that adjusts
        # the supervision boundaries.
        for tnfm in self.cut_transforms:
            cuts = tnfm(cuts)
        # Get a tensor with batched feature matrices, shape (B, T, F)
        # Collation performs auto-padding, if necessary.
        inputs, _ = self.input_strategy(cuts)
        masks = self.input_strategy.supervision_masks(
            cuts, use_alignment_if_exists='word'
        )
        # Apply all available transforms on the inputs, i.e. either audio or features.
        # This could be feature extraction, global MVN, SpecAugment, etc.
        for tnfm in self.input_transforms:
            inputs = tnfm(inputs)

        batch = {
            "inputs": inputs,
            "supervisions": {
                "sequence_idx": torch.Tensor(range(inputs.shape[0])),
                "is_voice": masks,
                "start_frame": torch.zeros(inputs.shape[0]),
                "duration": torch.Tensor(
                    [masks.shape[1] for _ in range(masks.shape[0])]
                ),
            },
        }
        if self.return_cuts:
            batch["supervisions"]["cut"] = cuts
        return batch

    def _validate(self) -> None:
        validate(self.cuts)
        # Unlike ASR, we allow supervisions starting before and ending after the
        # cut since the mask generation takes care of it.
