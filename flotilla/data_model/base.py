"""
Common operations performed on all kinds of data types
"""
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.preprocessing import StandardScaler

from ..compute.decomposition import DataFramePCA, DataFrameNMF
# from ..compute.clustering import Cluster
from ..compute.infotheory import binify
from ..compute.predict import PredictorConfigManager, PredictorDataSetManager
from ..visualize.decomposition import DecompositionViz
from ..visualize.generic import violinplot, nmf_space_transitions, \
    simple_twoway_scatter
from ..visualize.network import NetworkerViz
from ..visualize.predict import ClassifierViz
from ..util import memoize, cached_property
from ..compute.outlier import OutlierDetection

default_predictor_name = "ExtraTreesClassifier"
MINIMUM_FEATURE_SUBSET = 20


class BaseData(object):
    """Base class for biological data measurements. All data types in flotilla
    inherit from this

    Attributes
    ----------
    data : pandas.DataFrame
        A (n_samples, m_features) sized DataFrame of filtered input data, with
        features with too few samples (``minimum_samples``) detected at
        ``thresh`` removed. Compared to :py:attr:`.data_original`,
        ``m_features <= n_features`
    data_type : str
        String indicating what kind of data this is, e.g. "splicing" or
        "expression"
    data_original : pandas.DataFrame
        A (n_samples, n_features) sized DataFrame of all input data, before
        removing features for having too few samples
    feature_data : pandas.DataFrame
        A (k_features, n_features_about_features) sized DataFrame of features
        about the feature data. Notice that this DataFrame does not need to be
        the same size as the data, but must at least include all the features
        from :py:attr:`data`. Compared to :py:attr:`.data`,
        ``k_features >= m_features``
    feature_subsets : dict
        Dict of {"subset_name" : list_of_feature_ids} for feature subsets
        specified as either boolean columns in ``feature_data``. All columns in
        ``feature_ignore_subset_cols`` are ignored
    predictor_config_manager : PredictorConfigManager
        Manage different combinations of predictor on different data subtypes
    variant : pandas.Index
        Genes whose variance among all cells is 2 standard deviations away
        from the mean variance


    Methods
    -------
    feature_renamer
        If ``feature_rename_col`` is specified in :py:meth:`BaseData.__init__`,
        this will rename the feature ID to a new name. If
        ``feature_rename_col`` is not specified, then this will return the
        original id
    maybe_renamed_to_feature_id
        Convert a weird feature ID to your known gene names

    """

    def __init__(self, data, thresh=-np.inf,
                 minimum_samples=0,
                 feature_data=None,
                 feature_rename_col=None,
                 feature_ignore_subset_cols=None,
                 technical_outliers=None,
                 outliers=None,
                 pooled=None,
                 predictor_config_manager=None,
                 data_type=None):
        """Abstract base class for biological measurements

        Parameters
        ----------
        data : pandas.DataFrame
            A samples x features (samples on rows, features on columns)
            dataframe with some kind of measurements of cells,
            e.g. gene expression values such as TPM, RPKM or FPKM, alternative
            splicing "Percent-spliced-in" (PSI) values, or RNA editing scores.
            Note: If the columns are a multi-index, the "level 0" is assumed to
            be the unique, crazy ID like 'ENSG00000100320', and "level 1" is
            assumed to be the convenient gene name like "RBFOX2"
        thresh : float, optional (default=-np.inf)
            Minimum value to accept for this data.
        minimum_samples : int, optional (default=0)
            Minimum number of samples with values greater than ``thresh``.
            E.g., for use with "at least 3 single cells expressing the gene at
            greater than 1 TPM."
        feature_data : pandas.DataFrame, optional (default=None)
            A features x attributes dataframe of metadata about the features,
            e.g. annotating whether the gene is a housekeeping gene
        feature_rename_col : str, optional (default=None)
            Which column in the feature_data to use to rename feature IDs
            from a crazy ID to a common gene symbol, e.g. to transform
            'ENSG00000100320' into 'RBFOX2'
        feature_ignore_subset_cols : list-like (default=None)
            Columns in the feature data to ignore when making subsets,
            e.g. "gene_name" shouldn't be used to create subsets, since it's
            just a small number of them.
        technical_outliers : list-like, optional (default=None)
            List of sample IDs which should be completely ignored because
            they didn't pass the technical quality control
        outliers : list-like, optional (default=None)
            List of sample IDs which should be marked as outliers for
            plotting and interpretation purposes
        pooled : list-like, optional (default=None)
            List of sample IDs which should be marked as pooled for plotting
            and interpretation purposes.
        predictor_config_manager : PredictorConfigManager, optional
            (default=None)
            Object used to organize inputs to
            :py:class:`compute.predict.Regressor` and
            :py:class:`compute.predict.Classifier`. If None, one is initialized
            for this instance.
        data_type : str, optional (default=None)
            A string indicating what kind of data this is, e.g. "expression" or
            "splicing"

        Notes
        -----
        Any cells not marked as "technical_outliers", "outliers" or "pooled"
        are considered as single-cell samples.
        """
        self.data = data
        self.data_original = self.data
        self.thresh = thresh
        self.minimum_samples = minimum_samples
        self.data_type = data_type

        if technical_outliers is not None:
            sys.stderr.write("Removing technical outliers from consideration "
                             "in {0}:\n\t{1}\n".format(
                self.data_type, ", ".join(technical_outliers)))
            good_samples = ~self.data.index.isin(technical_outliers)
            self.data = self.data.ix[good_samples]

        self.pooled_samples = pooled if pooled is not None else []
        self.outlier_samples = outliers if outliers is not None else []
        self.single_samples = self.data.index[~self.data.index.isin(
            self.pooled_samples)]

        if self.thresh > -np.inf or self.minimum_samples > 0:
            self.data_original = self.data.copy()
            if not self.singles.empty:
                self.data = self._threshold(self.data, self.singles)
            else:
                self.data = self._threshold(self.data)

        self.feature_data = feature_data
        self.feature_ignore_subset_cols = [] if feature_ignore_subset_cols is \
                                                None else feature_ignore_subset_cols
        # if self.feature_data is None:
        # self.feature_data = pd.DataFrame(index=self.data.columns)
        self.feature_rename_col = feature_rename_col
        self.default_feature_sets = []

        if isinstance(self.data.columns, pd.MultiIndex):
            feature_ids, renamed = zip(*self.data.columns.values)
            self.feature_rename_col = 'gene_name'
            column = pd.Series(renamed, index=self.data.columns,
                               name=self.feature_rename_col)
            if self.feature_data is None:
                self.feature_data = pd.DataFrame(column,
                                                 index=self.data.columns)
            else:
                if self.feature_rename_col not in self.feature_data:
                    self.feature_data = self.feature_data.join(column,
                                                               rsuffix='_right')
                    if self.feature_rename_col + '_right' in self.feature_data:
                        self.feature_rename_col += '_right'

        if self.feature_data is not None and self.feature_rename_col is not \
                None:
            self.feature_renamer = \
                lambda x: self._shortener(x, renamer=self._feature_renamer)
        else:
            self.feature_renamer = self._shortener

        if predictor_config_manager is None:
            self.predictor_config_manager = PredictorConfigManager()
        else:
            self.predictor_config_manager = predictor_config_manager

        self.predictor_dataset_manager = PredictorDataSetManager(
            self.predictor_config_manager)

        self.networks = NetworkerViz(self)

    def _threshold(self, data, other=None):
        """Only take features with expression greater than the threshold,
        in at least the minimum number of samples.

        Parameters
        ----------
        data : pandas.DataFrame
            The data to filter, make smaller
        other : pandas.DataFrame, optional
            If provided, use this DataFrame to filter data. E.g. use the
            genes expressed in only single cells to filter the whole dataset.

        Returns
        -------
        filtered : pandas.DataFrame
            "data" filtered with expression values at least self.thresh
            in least self.minimum_samples
        """
        if other is None:
            other = data
        filtered = data.ix[:, other[other > self.thresh].count() >=
                              self.minimum_samples]
        return filtered

    def _feature_renamer(self, x):
        """Rename a feature from a crazy ID like 'ENSG00000100320' to 'RBFOX2'
        """
        if x in self.feature_renamer_series.index:
            rename = self.feature_renamer_series[x]
            if isinstance(rename, pd.Series):
                return rename.values[0]
            else:
                return rename
        else:
            return x

    @staticmethod
    def _shortener(x, renamer=None, max_char_len=20):
        """Shorten a feature ID to minimize the amount of messy text on plots

        Parameters
        ----------
        x : str
            A feature ID
        renamer : function, optional (default=None)
            A function to rename feature IDs to known gene symbols
        max_char_len : int, optional (default=20)
            Maximum length of the feature ids

        Returns
        -------
        shortened : str
            A potentially renamed, shortened string
        """
        if renamer is not None:
            renamed = renamer(x)
        else:
            renamed = x

        if isinstance(renamed, float):
            return renamed
        elif len(renamed) > max_char_len:
            return '{}...'.format(renamed[:max_char_len])
        else:
            return renamed

    @property
    def singles(self):
        """Data from only the single cells"""
        return self.data.ix[self.single_samples]

    @property
    def pooled(self):
        """Data from only the pooled samples"""
        return self.data.ix[self.pooled_samples]

    @property
    def outliers(self):
        """Data from only the outlier samples"""
        return self.data.ix[self.outlier_samples]

    @property
    def feature_renamer_series(self):
        """A pandas Series of the original feature ids to the renamed ids"""
        try:
            return self.feature_data[self.feature_rename_col].dropna()
        except (TypeError, ValueError):
            return pd.Series(self.data.columns.values,
                             index=self.data.columns)

    def maybe_renamed_to_feature_id(self, feature_id):
        """To be able to give a simple gene name, e.g. "RBFOX2" and get the
        official ENSG ids or MISO ids

        Parameters
        ----------
        feature_id : str
            The name of a feature ID. Could be either a common gene name, as in
            what the crazy IDs are :py:meth:`.feature_renamer` to, or

        Returns
        -------
        feature_id : str or list-like
            Valid Feature ID(s) that can be used to subset self.data
        """
        if feature_id in self.feature_renamer_series.values:
            feature_ids = self.feature_renamer_series[
                self.feature_renamer_series ==
                feature_id].index
            return self.data.columns.intersection(feature_ids)
        elif feature_id in self.data.columns:
            return feature_id
        else:
            raise ValueError('{} is not a valid feature identifier (it may '
                             'not have been measured in this dataset!)'
                             .format(feature_id))

    @property
    def _var_cut(self):
        """Variance values which are 2 standard deviations away the mean
        variance"""
        return self.data.var().dropna().mean() + 2 * self.data.var() \
            .dropna().std()

    @property
    def variant(self):
        """Genes whose variance among all cells is 2 standard deviations away
        from the mean variance"""
        return self.data.columns[self.data.var() > self._var_cut]

    def drop_outliers(self, data, outliers):
        """Remove outlier samples from this data"""
        not_in_index = set(outliers).difference(data.index)
        if len(not_in_index) >= 1:
            sys.stderr.write("These outlier samples are not in the index, "
                             "skipping them..."
                             "\n\t{}\n".format("\n\t".join(not_in_index)))
        outliers = set(outliers).intersection(data.index)

        try:
            # Remove pooled samples, if there are any
            pooled_cells = outliers.intersection(self.pooled.index)
            if len(pooled_cells) >= 1:
                sys.stderr.write("These samples are pooled,"
                                 "not outliers. Skipping..."
                                 "\n\t{}\n".format("\n\t".join(pooled_cells)))
            outliers = outliers.difference(self.pooled.index)
        except AttributeError:
            pass

        sys.stdout.write("dropping {}\n".format(outliers))
        data = data.drop(outliers)
        outlier_data = data.ix[outliers]
        return data, outlier_data

    @property
    def feature_subsets(self):
        """Dict of feature subset names to their list of feature ids"""
        feature_subsets = subsets_from_metadata(
            self.feature_data, MINIMUM_FEATURE_SUBSET, 'features',
            ignore=self.feature_ignore_subset_cols)
        feature_subsets['variant'] = self.variant
        return feature_subsets

    def feature_subset_to_feature_ids(self, feature_subset, rename=True):
        """Convert a feature subset name to a list of feature ids"""
        feature_ids = pd.Index([])
        if feature_subset is not None:
            try:
                if feature_subset in self.feature_subsets:
                    feature_ids = self.feature_subsets[feature_subset]
                elif feature_subset == 'all_genes':
                    feature_ids = self.data.columns
            except TypeError:
                if not isinstance(feature_subset, str):
                    feature_ids = feature_subset
                    n_custom = self.feature_data.columns.map(
                        lambda x: x.startswith('custom')).sum()
                    self.feature_data['custom_{}'.format(n_custom + 1)] = \
                        self.feature_data.index.isin(feature_ids)
                else:
                    raise ValueError(
                        "There are no {} features in this data: "
                        "{}".format(feature_subset, self))
            if rename:
                feature_ids = feature_ids.map(self.feature_renamer)
        else:
            feature_ids = self.data.columns
        return feature_ids

    # def calculate_distances(self, metric='euclidean'):
    #     """Creates a squareform distance matrix for clustering fun
    #
    #     Needed for some clustering algorithms
    #
    #     Parameters
    #     ----------
    #     metric : str, optional
    #         One of any valid scipy.distance metric strings. Default 'euclidean'
    #     """
    #     raise NotImplementedError
    #     self.pdist = squareform(pdist(self.binned, metric=metric))
    #     return self
    #
    # def correlate(self, method='spearman', between='features'):
    #     """Find correlations between either splicing/expression measurements
    #     or cells
    #
    #     Parameters
    #     ----------
    #     method : str
    #         Specify to calculate either 'spearman' (rank-based) or 'pearson'
    #         (linear) correlation. Default 'spearman'
    #     between : str
    #         Either 'features' or 'samples'. Default 'features'
    #     """
    #     raise NotImplementedError
    #     # Stub for choosing between features or samples
    #     if 'features'.startswith(between):
    #         pass
    #     elif 'samples'.startswith(between):
    #         pass
    #
    # def jsd(self):
    #     """Jensen-Shannon divergence showing most varying measurements within a
    #     celltype and between celltypes
    #     """
    #     raise NotImplementedError

    # TODO.md: Specify dtypes in docstring
    def plot_classifier(self, trait, sample_ids=None, feature_ids=None,
                        predictor_name=None, standardize=True,
                        score_coefficient=None, data_name=None, groupby=None,
                        label_to_color=None, label_to_marker=None, order=None,
                        color=None, **plotting_kwargs):
        """Classify samples on boolean or categorical traits

        Parameters
        ----------
        trait : pandas.Series
            A (n_samples,) series of categorical features. Must have the same
            index as :py:attr:`.data`
        sample_ids : list-like, optional (default=None)
            Which samples to use to classify
        feature_ids : list-like, optional (default=None)
            Which features to use
        predictor_name : str
            Name of the predictor to use, in
            :py:attr:`.predictor_config_manager`
        standardize : bool, optional (default=True)
            If True, mean-center the data so the mean of all features is 0,
            and divide by the standard deviation so the standard deviation of
            all features is 1. This allows us to compare lowly expressed
            features and highly expressed features on the same playing field
        data_name : str, optional (default=None)
            Name for this subset of the data
        groupby : mappable, optional (default=None)
            Map each sample id to a group, such as a phenotype label
        label_to_color : dict, optional (default=None)
            For each phenotype label, assign a color
        label_to_marker : dict, optional (default=None)
            For each phenotype label, assign a plotting marker symbol/shape
        order : list, optional (default=None)
            For violinplots, the order of the phenotype groups
        color : list, optional (default=None)
            For violinplots, the colors of the phenotypes in their order
        plotting_kwargs : other keyword arguments
            All other keyword arguments are passed to
            :py:meth:`.Classifier.__call__`, which passes them to
            :py:meth:`DecomopsitionViz.__call__`

        Returns
        -------
        self : BaseData
        """
        # print trait
        plotting_kwargs = {} if plotting_kwargs is None else plotting_kwargs

        # local_plotting_args = self.pca_plotting_args.copy()
        # local_plotting_args.update(plotting_kwargs)
        if predictor_name is None:
            predictor_name = default_predictor_name

        classifier = self.classify(trait, sample_ids=sample_ids,
                            feature_ids=feature_ids,
                            data_name=data_name,
                            standardize=standardize,
                            predictor_name=predictor_name,
                            groupby=groupby, label_to_marker=label_to_marker,
                            label_to_color=label_to_color, order=order,
                            color=color)

        if score_coefficient is not None:
            classifier.score_coefficient = score_coefficient
        classifier(**plotting_kwargs)
        return self

    def plot_dimensionality_reduction(self, x_pc=1, y_pc=2, sample_ids=None,
                                      feature_ids=None, featurewise=False,
                                      reducer=None, plot_violins=True,
                                      groupby=None, label_to_color=None,
                                      label_to_marker=None, order=None,
                                      reduce_kwargs=None, title='',
                                      **plotting_kwargs):
        """Principal component-like analysis of measurements

        Parameters
        ----------
        x_pc : int, optional (default=1)
            Which principal component to plot on the x-axis
        y_pc : int, optional (default=2)
            Which principal component to plot on the y-axis
        sample_ids : list, optional (default=None)
            If None, plot all the samples. If a list of strings, must be
            valid sample ids of the data.
        feature_ids : list, optional (default=None)
            If None, plot all the features. If a list of strings, perform and
            plot dimensionality reduction on only these feature ids
        featurewise : bool, optional (default=False)
            Whether to keep the features and reduce on the samples (default
            is to keep the samples and reduce the features)
        reducer : :py:class:`.DataFrameReducerBase`, optional
            (default=:py:class:`.DataFramePCA`)
            Which decomposition object to use. Must be a child of
            :py:class:`.DataFrameReducerBase` as this has built-in
            compatibility with pandas.DataFrames.
        plot_violins : bool, optional (default=True)
            If True, plot the violinplots of the top features. This
            can take a long time, so to save time you can turn it off if you
            just want a quick look at the PCA.
        groupby : mappable, optional (default=None)
            Map each sample id to a group, such as a phenotype label
        label_to_color : dict, optional (default=None)
            For each phenotype label, assign a color
        label_to_marker : dict, optional (default=None)
            For each phenotype label, assign a plotting marker symbol/shape
        order : list, optional (default=None)
            For violinplots, the order of the phenotype groups
        color : list, optional (default=None)
            For violinplots, the colors of the phenotypes in their order
        plotting_kwargs : other keyword arguments
            All other keyword arguments are passed to
            :py:meth:`DecomopsitionViz.__call__`

        Returns
        -------
        viz : :py:class:`.DecompositionViz`
            Object with plotted dimensionality reduction
        """
        reduce_kwargs = {} if reduce_kwargs is None else reduce_kwargs

        reduced = self.reduce(sample_ids, feature_ids,
                              featurewise=featurewise,
                              reducer=reducer, **reduce_kwargs)

        visualized = DecompositionViz(reduced.reduced_space,
                                      reduced.components_,
                                      reduced.explained_variance_ratio_,
                                      singles=self.singles,
                                      pooled=self.pooled,
                                      outliers=self.outliers,
                                      feature_renamer=self.feature_renamer,
                                      featurewise=featurewise,
                                      label_to_color=label_to_color,
                                      label_to_marker=label_to_marker,
                                      groupby=groupby, order=order,
                                      x_pc="pc_" + str(x_pc),
                                      y_pc="pc_" + str(y_pc))
        # pca(show_vectors=True,
        # **plotting_kwargs)
        return visualized(title=title,
                          plot_violins=plot_violins, **plotting_kwargs)

    def plot_pca(self, **kwargs):
        """Call ``plot_dimensionality_reduction`` with PCA specifically"""
        return self.plot_dimensionality_reduction(reducer=DataFramePCA,
                                                  **kwargs)

    def _subset(self, data, sample_ids=None, feature_ids=None,
                require_min_samples=True):
        """Smartly subset the data given sample and feature ids

        Take only a subset of the data, and require at least the minimum
        samples observed to be not NA for each feature.

        Parameters
        ----------
        data : pandas.DataFrame
            Data to subset
        sample_ids : list-like, optional (default=None)
            Which samples to use. If None, use all.
        feature_ids : list-like, optional (default=None)
            Which features to use. If None, use all.
        require_min_samples : bool, optional (default=True)
            If True, then require `minimum_samples` for each feature

        Returns
        -------
        subset : pandas.DataFrame
            The subset of data with only these sample ids and feature ides
        """
        if feature_ids is None:
            feature_ids = data.columns
        if sample_ids is None:
            sample_ids = data.index

        sample_ids = pd.Index(set(sample_ids).intersection(data.index))
        feature_ids = pd.Index(set(feature_ids).intersection(data.columns))

        if len(sample_ids) == 1:
            sample_ids = sample_ids[0]

        if len(feature_ids) == 1:
            feature_ids = feature_ids[0]
            single_feature = True
        else:
            single_feature = False

        subset = data.ix[sample_ids]
        subset = subset.T.ix[feature_ids].T

        if require_min_samples and not single_feature:
            subset = subset.ix[:, subset.count() >= self.minimum_samples]

        if subset.empty:
            raise ValueError('This data subset is empty. Please double-check '
                             'that the gene ids are for the correct species!')
        return subset

    def _subset_singles_and_pooled(self, sample_ids=None,
                                   feature_ids=None):
        """Subset singles and pooled, taking only features that appear in both

        Parameters
        ----------
        sample_ids : list-like, optional (default=None)
            List of samples to use. If None, use all. If none of the sample ids
            overlap with pooled samples, will assume you want all the pooled
            samples
        feature_ids : list-like, optional (default=None)
            List of feature ids to use. If None, use all

        Returns
        -------
        singles : pandas.DataFrame
            DataFrame of only single-cell samples, with only features that
            appear in both these single cell and pooled samples
        pooled : pandas.DataFrame
            DataFrame of only pooled samples, with only features that appear
            in both these single cell and pooled samples
        """
        singles = self._subset(self.data, sample_ids, feature_ids,
                               require_min_samples=True)
        try:
            # If the sample ids don't overlap with the pooled sample, assume
            # you want all the pooled samples
            if sample_ids is not None and sum(
                    self.pooled.index.isin(sample_ids)) \
                    > 0:
                pooled_sample_ids = sample_ids
            else:
                pooled_sample_ids = None
            pooled = self._subset(self.pooled, pooled_sample_ids, feature_ids,
                                  require_min_samples=False)
            if feature_ids is None or len(feature_ids) > 1:
                # These are DataFrames
                singles, pooled = singles.align(pooled, axis=1, join='inner')
            else:
                # These are Seriessssss
                singles = singles.dropna()
                pooled = pooled.dropna()
        except AttributeError:
            pooled = None

        return singles, pooled

    def _subset_and_standardize(self, data, sample_ids=None,
                                feature_ids=None,
                                standardize=True, return_means=False,
                                rename=False):

        """Take only the sample ids and feature ids from this data, require
        at least some minimum samples, and standardize data using
        scikit-learn. Will also fill na values with the mean of the feature
        (column)

        Parameters
        ----------
        data : pandas.DataFrame
            The data you want to standardize
        sample_ids : list-like, optional (default=None)
            If None, all sample ids will be used, else only the sample ids
            specified
        feature_ids : list-like, optional (default=None)
            If None, all features will be used, else only the features
            specified
        standardize : bool, optional (default=True)
            Whether or not to "whiten" (make all variables uncorrelated) and
            mean-center via sklearn.preprocessing.StandardScaler
        return_means : bool, optional (default=False)
            If True, return a tuple of (subset, means), otherwise just return
            the subset
        rename : bool, optional (default=False)
            Whether or not to rename the feature ids using ``feature_renamer``

        Returns
        -------
        subset : pandas.DataFrame
            Subset of the dataframe with the requested samples and features,
            and standardized as described
        means : pandas.DataFrame
            (Only if return_means=True) Mean values of the features (columns).
        """
        # fill na with mean for each event
        subset = self._subset(data, sample_ids, feature_ids)
        means = subset.mean()
        subset = subset.fillna(means).fillna(0)

        if rename:
            means = means.rename_axis(self.feature_renamer)
            subset = subset.rename_axis(self.feature_renamer, 1)

        # whiten, mean-center
        if standardize:
            data = StandardScaler().fit_transform(subset)
        else:
            data = subset

        # "data" is a matrix so need to transform it back into a convenient
        # dataframe
        subset = pd.DataFrame(data, index=subset.index,
                              columns=subset.columns)
        if return_means:
            return subset, means
        else:
            return subset

    # def plot_clusteredheatmap(self, sample_ids, feature_ids,
    #                           metric='euclidean',
    #                           linkage_method='average',
    #                           sample_colors=None,
    #                           feature_colors=None, figsize=None,
    #                           require_min_samples=True):
    #     """
    #
    #     """
    #     subset, row_linkage, col_linkage = self._calculate_linkage(
    #         sample_ids, feature_ids, linkage_method=linkage_method,
    #         metric=metric)
    #
    #     if figsize is None:
    #         figsize = reversed(subset.shape)
    #         figsize = map(lambda x: max(.25 * x, 1000), figsize)
    #
    #     col_kws = dict(linkage_matrix=col_linkage, side_colors=feature_colors,
    #                    label=map(self.feature_renamer, subset.columns))
    #     row_kws = dict(linkage_matrix=row_linkage, side_colors=sample_colors)
    #     return sns.clusteredheatmap(subset, row_kws=row_kws, col_kws=col_kws,
    #                                 pcolormesh_kws=dict(linewidth=0.01),
    #                                 figsize=figsize)

    #@memoize
    def detect_outliers(self,
                        sample_ids=None, feature_ids=None,
                        featurewise=False,
                        reducer=None,
                        standardize=True,
                        reducer_kwargs=None,
                        bins=None,
                        outlier_detection_method=None,
                        outlier_detection_method_kwargs=None):

        default_reducer_args = {"n_components": 2}

        if reducer_kwargs is None:
            reducer_kwargs = default_reducer_args
        else:
            default_reducer_args.update(reducer_kwargs)
            reducer_kwargs = default_reducer_args

        reducer = self.reduce(sample_ids, feature_ids,
                              featurewise, reducer,
                              standardize, reducer_kwargs,
                              bins)

        outlier_detector = OutlierDetection(reducer.reduced_space,
                                            method=outlier_detection_method,
                                            **outlier_detection_method_kwargs)

        return reducer, outlier_detector

    def plot_outliers(self, reducer, outlier_detector, **pca_args):
        show_point_labels = pca_args['show_point_labels']
        del pca_args['show_point_labels']
        dv = DecompositionViz(reducer.reduced_space,
                              reducer.components_,
                              reducer.explained_variance_ratio_,
                              groupby=outlier_detector.outliers,
                              )

        dv(show_point_labels=show_point_labels)
        #self.plot_pca(self, groupby=outlier_detector.outliers,
        #              title=outlier_detector.title,
        #              **pca_args)

        # DecompositionViz(reducer.reduced_space,
        # reducer.components_,
        #                       reducer.explained_variance_ratio_,
        #                       DataModel=self,
        #                       feature_renamer=feature_renamer,
        #                       groupby=outlier_detector.outliers,
        #                       featurewise=False,
        #                       order=None, violinplot_kws=None,
        #                       data_type=None, label_to_color=None,
        #                       label_to_marker=None,
        #                       scale_by_variance=True, x_pc=x_pc,
        #                       y_pc=y_pc, n_vectors=0, distance='L1',
        #                       n_top_pc_features=50)
        #dv(show_point_labels=show_point_labels, title=outlier_detector.title)

    # @memoize
    def reduce(self, sample_ids=None, feature_ids=None,
               featurewise=False,
               reducer=None,
               standardize=None,
               reducer_kwargs=None, bins=None):
        """Make and memoize a reduced dimensionality representation of data

        Parameters
        ----------
        data : pandas.DataFrame
            samples x features data to reduce
        sample_ids : None or list of strings
            If None, all sample ids will be used, else only the sample ids
            specified
        feature_ids : None or list of strings
            If None, all features will be used, else only the features
            specified
        featurewise : bool
            Whether or not to use the features as the "samples", e.g. if you
            want to reduce the features in to "sample-space" instead of
            reducing the samples into "feature-space"
        standardize : bool
            Whether or not to "whiten" (make all variables uncorrelated) and
            mean-center via sklearn.preprocessing.StandardScaler
        title : str
            Title of the plot
        reducer_kwargs : dict
            Any additional arguments to send to the reducer

        Returns
        -------
        reducer_object : flotilla.compute.reduce.ReducerViz
            A ready-to-plot object containing the reduced space
        """
        if reducer is None:
            reducer = DataFramePCA

        if standardize is None:
            standardize = True

        reducer_kwargs = {} if reducer_kwargs is None else reducer_kwargs

        subset, means = self._subset_and_standardize(self.data,
                                                     sample_ids, feature_ids,
                                                     standardize,
                                                     return_means=True)
        if bins is not None:
            subset = self.binify(subset, bins)

        # compute reduction
        if featurewise:
            subset = subset.T

        reducer_object = reducer(subset, **reducer_kwargs)
        reducer_object.means = means
        return reducer_object

    def classify(self, trait, sample_ids, feature_ids,
                 standardize=True,
                 data_name='expression',
                 predictor_name='ExtraTreesClassifier',
                 predictor_obj=None,
                 predictor_scoring_fun=None,
                 score_cutoff_fun=None,
                 n_features_dependent_kwargs=None,
                 constant_kwargs=None,
                 plotting_kwargs=None,
                 color=None, groupby=None, label_to_color=None,
                 label_to_marker=None, order=None, bins=None):
        """Make and memoize a predictor on a categorical trait (associated
        with samples) subset of genes

        Parameters
        ----------
        trait : pandas.Series
            samples x categorical feature
        sample_ids : None or list of strings
            If None, all sample ids will be used, else only the sample ids
            specified
        feature_ids : None or list of strings
            If None, all features will be used, else only the features
            specified
        standardize : bool
            Whether or not to "whiten" (make all variables uncorrelated) and
            mean-center and make unit-variance all the data via sklearn
            .preprocessing.StandardScaler
        predictor : flotilla.visualize.predict classifier
            Must inherit from flotilla.visualize.PredictorBaseViz. Default is
            flotilla.visualize.predict.ClassifierViz
        predictor_kwargs : dict or None
            Additional 'keyword arguments' to supply to the predictor class
        predictor_scoring_fun : function
            Function to get the feature scores for a scikit-learn classifier.
            This can be different for different classifiers, e.g. for a
            classifier named "x" it could be x.scores_, for other it's
            x.feature_importances_. Default: lambda x: x.feature_importances_
        score_cutoff_fun : function
            Function to cut off insignificant scores
            Default: lambda scores: np.mean(x) + 2 * np.std(x)

        Returns
        -------
        predictor : flotilla.compute.predict.PredictorBaseViz
            A ready-to-plot object containing the predictions
        """
        subset = self._subset_and_standardize(self.data, sample_ids,
                                              feature_ids, standardize)
        # subset.rename_axis(self.feature_renamer, 1, inplace=True)
        plotting_kwargs = {} if plotting_kwargs is None else plotting_kwargs

        classifier = ClassifierViz(
            data_name, trait.name, predictor_name=predictor_name,
            X_data=subset, trait=trait, predictor_obj=predictor_obj,
            predictor_scoring_fun=predictor_scoring_fun,
            score_cutoff_fun=score_cutoff_fun,
            n_features_dependent_kwargs=n_features_dependent_kwargs,
            constant_kwargs=constant_kwargs,
            predictor_dataset_manager=self.predictor_dataset_manager,
            data_type=self.data_type, color=color,
            groupby=groupby, label_to_color=label_to_color,
            label_to_marker=label_to_marker, order=order,
            feature_renamer=self.feature_renamer,
            singles=self.singles, pooled=self.pooled, outliers=self.outliers,
            **plotting_kwargs)
        return classifier

    def _calculate_linkage(self, data, sample_ids, feature_ids,
                           metric='euclidean',
                           linkage_method='median', standardize=True,
                           require_min_samples=True):

        subset = self._subset_and_standardize(data, sample_ids,
                                              feature_ids,
                                              standardize=standardize)
        row_linkage, col_linkage = self.clusterer(subset, metric,
                                                  linkage_method)
        return subset, row_linkage, col_linkage

    def binify(self, data, bins=None):
        return binify(data, bins).dropna(how='all', axis=0).dropna(how='all',
                                                                   axis=1)


    def _violinplot(self, feature_id, sample_ids=None,
                    phenotype_groupby=None,
                    phenotype_order=None, ax=None, color=None,
                    label_pooled=False):
        """For compatiblity across data types, can specify _violinplot
        """
        singles, pooled = self._subset_singles_and_pooled(sample_ids,
                                                          feature_ids=[
                                                              feature_id])
        outliers = None
        try:
            if not self.outliers.empty:
                outliers = self._subset(self.outliers,
                                        feature_ids=[feature_id])
        except AttributeError:
            pass

        renamed = self.feature_renamer(feature_id)
        # if isinstance(self.data.columns, pd.MultiIndex):
        # feature_id, renamed = feature_id
        # else:
        # renamed = self.feature_renamer(feature_id)
        title = '{}\n{}'.format(renamed, ':'.join(
            feature_id.split('@')[0].split(':')[:2]))

        violinplot(singles, groupby=phenotype_groupby, color_ordered=color,
                   pooled_data=pooled, order=phenotype_order,
                   title=title, data_type=self.data_type, ax=ax,
                   label_pooled=label_pooled, outliers=outliers)

    @cached_property()
    def nmf(self):
        data = self._subset(self.data)
        return DataFrameNMF(self.binify(data).T, n_components=2)

    @memoize
    def binned_nmf_reduced(self, sample_ids=None, feature_ids=None):
        data = self._subset(self.data, sample_ids, feature_ids,
                            require_min_samples=False)
        binned = self.binify(data)
        reduced = self.nmf.transform(binned.T)
        return reduced

    def plot_feature(self, feature_id, sample_ids=None,
                     phenotype_groupby=None,
                     phenotype_order=None, color=None,
                     phenotype_to_color=None,
                     phenotype_to_marker=None, xlabel=None, ylabel=None,
                     nmf_space=False):
        """
        Plot the violinplot of a splicing event (should also show NMF movement)
        """
        feature_ids = self.maybe_renamed_to_feature_id(feature_id)

        if not isinstance(feature_ids, pd.Index):
            feature_ids = [feature_id]

        ncols = 2 if nmf_space else 1

        for feature_id in feature_ids:
            fig, axes = plt.subplots(ncols=ncols, figsize=(4 * ncols, 4))
            if not nmf_space:
                axes = [axes]
            # if self.data_type == 'expression':
            # axes = [axes]

            self._violinplot(feature_id, sample_ids=sample_ids,
                             phenotype_groupby=phenotype_groupby,
                             phenotype_order=phenotype_order, ax=axes[0],
                             color=color)
            # if self.data_type == 'splicing':
            if nmf_space:
                try:
                    self.plot_nmf_space_transitions(
                        feature_id, groupby=phenotype_groupby,
                        phenotype_to_color=phenotype_to_color,
                        phenotype_to_marker=phenotype_to_marker,
                        order=phenotype_order, ax=axes[1],
                        xlabel=xlabel, ylabel=ylabel)
                except KeyError:
                    continue
            sns.despine()

    def nmf_space_positions(self, groupby, min_samples_per_group=5):
        data = self.data.groupby(groupby).filter(
            lambda x: len(x) >= min_samples_per_group)
        df = data.groupby(groupby).apply(
            lambda x: self.binned_nmf_reduced(sample_ids=x.index))
        df = df.swaplevel(0, 1)
        df = df.sort_index()
        return df

    def plot_nmf_space_transitions(self, feature_id, groupby,
                                   phenotype_to_color,
                                   phenotype_to_marker, order, ax=None,
                                   xlabel=None, ylabel=None):
        nmf_space_positions = self.nmf_space_positions(groupby)

        nmf_space_transitions(nmf_space_positions, feature_id,
                              phenotype_to_color,
                              phenotype_to_marker, order,
                              ax, xlabel, ylabel)

    @staticmethod
    def transition_distances(df, transitions):
        df.index = df.index.droplevel(0)
        distances = pd.Series(index=transitions)
        for transition in transitions:
            try:
                phenotype1, phenotype2 = transition
                norm = np.linalg.norm(df.ix[phenotype2] - df.ix[phenotype1])
                # print phenotype1, phenotype2, norm
                distances[transition] = norm
            except KeyError:
                pass
        return distances

    def big_nmf_space_transitions(self, groupby, phenotype_transitions):
        nmf_space_positions = self.nmf_space_positions(groupby)
        nmf_space_transitions = nmf_space_positions.groupby(
            level=0, axis=0, as_index=False, group_keys=False).apply(
            self.transition_distances,
            transitions=phenotype_transitions)

        mean = nmf_space_transitions.mean()
        std = nmf_space_transitions.std()
        big_transitions = nmf_space_transitions[
            nmf_space_transitions > (mean + 2 * std)].dropna(how='all')
        return big_transitions

    def plot_big_nmf_space_transitions(self, phenotype_groupby,
                                       phenotype_transitions,
                                       phenotype_order, color,
                                       phenotype_to_color,
                                       phenotype_to_marker):
        big_transitions = self.big_nmf_space_transitions(phenotype_groupby,
                                                         phenotype_transitions)
        for feature_id in big_transitions.index:
            self.plot_feature(feature_id, phenotype_groupby=phenotype_groupby,
                              phenotype_order=phenotype_order, color=color,
                              phenotype_to_color=phenotype_to_color,
                              phenotype_to_marker=phenotype_to_marker,
                              nmf_space=True)


    def plot_two_samples(self, sample1, sample2, **kwargs):
        """

        Parameters
        ----------
        sample1 : str
            Name of the sample to plot on the x-axis
        sample2 : str
            Name of the sample to plot on the y-axis
        Any other keyword arguments valid for seaborn.jointplot

        Returns
        -------
        jointgrid : seaborn.axisgrid.JointGrid
            Returns a JointGrid instance

        See Also
        -------
        seaborn.jointplot

        """
        x = self.data.ix[sample1]
        y = self.data.ix[sample2]
        return simple_twoway_scatter(x, y, **kwargs)


def subsets_from_metadata(metadata, minimum, subset_type, ignore=None):
    """

    Parameters
    ----------
    metadata : pandas.DataFrame
        The dataframe whose columns to use to create subsets of the rows
    minimum : int
        Minimum number of rows required for a column or group in the column
        to be included
    subset_type : str
        The name of the kind of subset. e.g. "samples" or "features"
    ignore : list-like
        List of columns to ignore

    Returns
    -------
    subsets : dict
        A name: row_ids mapping of which samples correspond to which group
    """
    subsets = {}
    ignore = () if ignore is None else ignore
    if metadata is not None:
        for col in metadata:
            if col in ignore:
                continue
            if metadata[col].dtype == bool:
                sample_subset = metadata.index[metadata[col]]
                subsets[col] = sample_subset
            else:
                grouped = metadata.groupby(col)
                sizes = grouped.size()
                filtered_sizes = sizes[sizes >= minimum]
                for group in filtered_sizes.keys():
                    if isinstance(group, bool):
                        continue
                    name = '{}: {}'.format(col, group)
                    subsets[name] = grouped.groups[group]
        for sample_subset in subsets.keys():
            name = 'not ({})'.format(sample_subset)
            if 'False' in name or 'True' in name:
                continue
            if name not in subsets:
                in_features = metadata.index.isin(subsets[
                    sample_subset])
                subsets[name] = metadata.index[~in_features]
        subsets['all {}'.format(subset_type)] = metadata.index
    return subsets

