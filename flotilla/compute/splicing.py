import collections

import numpy as np
import pandas as pd
from sklearn import cross_validation

from ..util import memoize
from .infotheory import jsd, binify


class Modalities(object):
    """Estimate the modality of a splicing event

    This is based off of the "percent spliced-in" (PSI) score of a splicing
    event, for example in a cassette exon event, how many transcripts use the
    cassette exon gives the "PSI" (:math:`\Psi`) score of that splicing event

    Possible modalities include:
    - Excluded (most cells have excluded the exon)
    - Middle (most cells have both the included and excluded isoforms)
    - Included (most cells have included the exon)
    - Bimodal (approximately a 50:50 distribution of inclusoion:exclusion)
    - Uniform (uniform distribution of exon usage)

    The way that these modalities are calculated is by binning each splicing
    event across all cells from (0, excluded_max, included_min, 1), and finding
    the Jensen-Shannon Divergence that event, and each of the five modalities

    Parameters
    ----------
    excluded_max : float, optional (default=0.2)
        Maximum value of excluded bin
    included_min : float, optional (default=0.8)
        Minimum value of included bin

    Attributes
    ----------


    Methods
    -------
    """
    modalities_bins = np.array([[1, 0, 0],  # excluded
                                [0, 1, 0],  # middle
                                [0, 0, 1],  # included
                                [1, 0, 1],  # bimodal
                                [1, 1, 1]])  # uniform

    modalities_names = ['excluded', 'middle', 'included', 'bimodal',
                        'uniform']

    true_modalities = pd.DataFrame(modalities_bins.T, columns=modalities_names)

    def __init__(self, excluded_max=0.2, included_min=0.8):
        self.bins = (0, excluded_max, included_min, 1)

    def _col_jsd_modalities(self, col):
        """Calculate JSD between a single binned event and true modalities

        Given a column of a binned splicing event, calculate its Jensen-Shannon
        divergence to the true modalities

        Parameters
        ----------
        col : pandas.Series
            A (n_bins,) sized series of a splicing event

        Returns
        -------
        jsd : pandas.Series
            A (n_modalities,) sized series of JSD between this event and the
            true modalities
        """
        return self.true_modalities.apply(jsd, axis=0, p=col)

    def sqrt_jsd_modalities(self, binned):
        """Calculate JSD between all binned splicing events and true modalities

        Use square root of JSD because it's a metric.

        Parameters
        ----------
        binned : pandas.DataFrame
            A (n_bins, n_events) sized DataFrame of binned splicing events

        Returns
        -------
        sqrt_jsd : pandas.DataFrame
            A (n_modalities, n_events) sized DataFrame of the square root JSD
            between splicing events and all modalities

        """
        return np.sqrt(binned.apply(self._col_jsd_modalities, axis=0))

    def assignments(self, sqrt_jsd_modalities):
        """Return the modality with the smallest square root JSD to each event

        Parameters
        ----------
        sqrt_jsd_modalities : pandas.DataFrame
            A modalities x features dataframe of the square root
            Jensen-Shannon divergence between this event and each modality

        Returns
        -------
        assignments : pandas.Series
            The closest modality to each splicing event
        """
        modalities = self.true_modalities.columns[
            np.argmin(sqrt_jsd_modalities.values, axis=0)]
        return pd.Series(modalities, sqrt_jsd_modalities.columns)

    @memoize
    def fit_transform(self, data, bootstrapped=False, bootstrapped_kws=None):
        """Given psi scores, estimate the modality of each

        Parameters
        ----------
        data : pandas.DataFrame
            A samples x features dataframe, where you want to find the
            splicing modality of each column (feature)
        bootstrapped : bool
            Whether or not to use bootstrapping, i.e. resample each splicing
            event several times to get a better estimate of its true modality.
            Default False.
        bootstrappped_kws : dict
            Valid arguments to _bootstrapped_fit_transform. If None, default is
            dict(n_iter=100, thresh=0.6, minimum_samples=10)

        Returns
        -------
        assignments : pandas.Series
            Modality assignments of each column (feature)
        """
        if bootstrapped:
            bootstrapped_kws = {} if bootstrapped_kws \
                                     is None else bootstrapped_kws
            return self._bootstrapped_fit_transform(data, **bootstrapped_kws)
        else:
            return self._single_fit_transform(data)

    def _single_fit_transform(self, data, do_not_memoize=False):
        """Given psi scores, estimate the modality of each

        Parameters
        ----------
        data : pandas.DataFrame
            A samples x features dataframe, where you want to find the
            splicing modality of each column (feature)
        do_not_memoize : bool
            Whether or not to memoize the results of the _single_fit_transform
            on this data (used by @memoize decorator)

        Returns
        -------
        assignments : pandas.Series
            Modality assignments of each column (feature)
        """
        binned = binify(data, self.bins)
        self.true_modalities.index = binned.index
        return self.assignments(self.sqrt_jsd_modalities(binned))

    def _bootstrapped_fit_transform(self, data, n_iter=100, thresh=0.6,
                                    min_samples=10):
        """Resample each splicing event n_iter times to robustly estimate
        modalities.
        """
        bs = cross_validation.Bootstrap(data.shape[0], n_iter=n_iter)

        assignments = pd.DataFrame(columns=data.columns,
                                   index=range(n_iter))

        for i, (train_index, test_index) in enumerate(bs):
            index = train_index + test_index
            psi = data.ix[data.index[index], :]
            psi = psi.dropna(axis=1, thresh=min_samples)
            assignments.ix[i] = self._single_fit_transform(psi,
                                                           do_not_memoize=True)

        counts = assignments.apply(lambda x: pd.Series(
            collections.Counter(x.dropna())))
        fractions = counts / counts.sum().astype(float)
        thresh_assignments = fractions[fractions >= thresh].apply(
            self._max_assignment, axis=0)
        thresh_assignments = thresh_assignments.fillna('unassigned')
        return thresh_assignments

    @staticmethod
    def _max_assignment(x):
        """Given a pandas.Series of modalities counts, return the maximum
        value. Necessary because just np.argmax will use np.nan as the max :(

        Parameters
        ----------
        x : pandas.Series
            A (n_modalities,) sized pandas series of integers, which count how
            many times this single splicing event was assigned to a modality.

        Returns
        -------
        assignment : str
            The modality with the largest number of counts
        """
        if np.isfinite(x).sum() == 0:
            return np.nan
        else:
            return np.argmax(x)

    def counts(self, psi, bootstrapped=False, bootstrapped_kws=None):
        """Return the number of events in each modality category

        Parameters
        ----------
        psi : pandas.DataFrame
            A samples x features dataframe of psi scores of a splicing event

        Returns
        -------
        counts : pandas.Series
            Counts of each modality
        """
        assignments = self.fit_transform(psi, bootstrapped, bootstrapped_kws)
        return assignments.groupby(assignments).size()


def switchy_score(array):
    """Transform a 1D array of data scores to a vector of "switchy scores"

    Calculates std deviation and mean of sine- and cosine-transformed
    versions of the array. Better than sorting by just the mean which doesn't
    push the really lowly variant events to the ends.

    Parameters
    ----------
    array : numpy.array
        A 1-D numpy array or something that could be cast as such (like a list)

    Returns
    -------
    float
        The "switchy score" of the study_data which can then be compared to
        other splicing event study_data

    """
    array = np.array(array)
    variance = 1 - np.std(np.sin(array[~np.isnan(array)] * np.pi))
    mean_value = -np.mean(np.cos(array[~np.isnan(array)] * np.pi))
    return variance * mean_value


def get_switchy_score_order(x):
    """Apply switchy scores to a 2D array of data scores

    Parameters
    ----------
    x : numpy.array
        A 2-D numpy array in the shape [n_events, n_samples]

    Returns
    -------
    numpy.array
        A 1-D array of the ordered indices, in switchy score order
    """
    switchy_scores = np.apply_along_axis(switchy_score, axis=0, arr=x)
    return np.argsort(switchy_scores)
