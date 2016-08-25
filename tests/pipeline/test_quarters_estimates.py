import blaze as bz
import itertools
from nose_parameterized import parameterized
import numpy as np
import pandas as pd

from zipline.pipeline import SimplePipelineEngine, Pipeline, CustomFactor
from zipline.pipeline.common import (
    EVENT_DATE_FIELD_NAME,
    FISCAL_QUARTER_FIELD_NAME,
    FISCAL_YEAR_FIELD_NAME,
    SID_FIELD_NAME,
    TS_FIELD_NAME,
)
from zipline.pipeline.data import DataSet, Column
from zipline.pipeline.loaders.blaze.estimates import (
    BlazeNextEstimatesLoader,
    BlazePreviousEstimatesLoader
)
from zipline.pipeline.loaders.quarter_estimates import (
    NextQuartersEstimatesLoader,
    PreviousQuartersEstimatesLoader,
    split_normalized_quarters, normalize_quarters)
from zipline.testing import ZiplineTestCase
from zipline.testing.fixtures import WithAssetFinder, WithTradingSessions
from zipline.testing.predicates import assert_equal
from zipline.utils.numpy_utils import datetime64ns_dtype, float64_dtype


class Estimates(DataSet):
    event_date = Column(dtype=datetime64ns_dtype)
    fiscal_quarter = Column(dtype=float64_dtype)
    fiscal_year = Column(dtype=float64_dtype)
    estimate = Column(dtype=float64_dtype)


def QuartersEstimates(num_qtr):
    class QtrEstimates(Estimates):
        num_quarters = num_qtr
        name = Estimates
    return QtrEstimates


# 0Q1: 2015-01-05.Q1.e1.2015-01-06, 2015-01-10.Q1.e1.2015-01-11,
# 0Q2: 2015-01-15.Q2.e1.2015-01-16, 2015-01-20.Q2.e1.2015-01-21,
# 0Q3: 2015-01-25.Q3.e1.2015-01-26, 2015-01-30.Q3.e1.2015-01-31,
# 0Q4: 2015-02-05.Q4.e1.2015-02-06, 2015-02-10.Q4.e1.2015-02-11,
estimates_timeline = pd.DataFrame({
    TS_FIELD_NAME: [pd.Timestamp('2015-01-05'), pd.Timestamp('2015-01-07'),
                    pd.Timestamp('2015-01-05'), pd.Timestamp('2015-01-17'),
                    pd.Timestamp('2015-01-05'), pd.Timestamp('2015-01-17'),
                    pd.Timestamp('2015-01-22'),
                    pd.Timestamp('2015-01-05'), pd.Timestamp('2015-01-17'),
                    pd.Timestamp('2015-01-22'), pd.Timestamp('2015-02-02')],
    EVENT_DATE_FIELD_NAME:
        [pd.Timestamp('2015-01-10'), pd.Timestamp('2015-01-10'),
         pd.Timestamp('2015-01-20'), pd.Timestamp('2015-01-20'),
         pd.Timestamp('2015-01-30'), pd.Timestamp('2015-01-30'),
         pd.Timestamp('2015-01-30'),
         pd.Timestamp('2015-02-10'), pd.Timestamp('2015-02-10'),
         pd.Timestamp('2015-02-10'), pd.Timestamp('2015-02-10')],
    'estimate': [1.]*2 + [2.] * 2 + [3.] * 3 + [4.] * 4,
    FISCAL_QUARTER_FIELD_NAME: [1]*2 + [2] * 2 + [3] * 3 + [4] * 4,
    FISCAL_YEAR_FIELD_NAME: [2015]*11,
    SID_FIELD_NAME: [0]*11
})


# Final release dates never change. The quarters have very tight date ranges
# in order to reduce the number of dates we need to iterate through when
# testing.
releases = pd.DataFrame({
    TS_FIELD_NAME: [pd.Timestamp('2015-01-15'), pd.Timestamp('2015-01-31')],
    EVENT_DATE_FIELD_NAME: [pd.Timestamp('2015-01-15'),
                            pd.Timestamp('2015-01-31')],
    'estimate': [0.5, 0.8],
    FISCAL_QUARTER_FIELD_NAME: [1.0, 2.0],
    FISCAL_YEAR_FIELD_NAME: [2015.0, 2015.0]
})

q1_knowledge_dates = [pd.Timestamp('2015-01-01'), pd.Timestamp('2015-01-04'),
                      pd.Timestamp('2015-01-08'), pd.Timestamp('2015-01-12')]
q2_knowledge_dates = [pd.Timestamp('2015-01-16'), pd.Timestamp('2015-01-20'),
                      pd.Timestamp('2015-01-24'), pd.Timestamp('2015-01-28')]
# We want to model the possibility of an estimate predicting a release date
# that doesn't match the actual release. This could be done by dynamically
# generating more combinations with different release dates, but that
# significantly increases the amount of time it takes to run the tests. These
# hard-coded cases are sufficient to know that we can update our beliefs when
# we get new information.
q1_release_dates = [pd.Timestamp('2015-01-15'),
                    pd.Timestamp('2015-01-16')]  # One day late
q2_release_dates = [pd.Timestamp('2015-01-30'),  # One day early
                    pd.Timestamp('2015-01-31')]
estimates = pd.DataFrame({
    EVENT_DATE_FIELD_NAME: q1_release_dates + q2_release_dates,
    'estimate': [.1, .2, .3, .4],
    FISCAL_QUARTER_FIELD_NAME: [1.0, 1.0, 2.0, 2.0],
    FISCAL_YEAR_FIELD_NAME: [2015.0, 2015.0, 2015.0, 2015.0]
})


def gen_estimates():
    sid_estimates = []
    sid_releases = []
    for sid, (q1e1, q1e2, q2e1, q2e2) in enumerate(
            itertools.permutations(q1_knowledge_dates + q2_knowledge_dates,
                                   4)
    ):
        # We're assuming that estimates must come before the relevant release.
        if (q1e1 < q1e2 and
                q2e1 < q2e2 and
                q1e1 < q1_release_dates[0] and
                q1e2 < q1_release_dates[1]):
            sid_estimate = estimates.copy(True)
            sid_estimate[TS_FIELD_NAME] = [q1e1, q1e2, q2e1, q2e2]
            sid_estimate[SID_FIELD_NAME] = sid
            sid_estimates += [sid_estimate]
            sid_release = releases.copy(True)
            sid_release[SID_FIELD_NAME] = sid_estimate[SID_FIELD_NAME]
            sid_releases += [sid_release]
    return pd.concat(sid_estimates + sid_releases).reset_index(drop=True)


class EstimateTestCase(WithAssetFinder,
                       WithTradingSessions,
                       ZiplineTestCase):
    START_DATE = pd.Timestamp('2014-12-28')
    END_DATE = pd.Timestamp('2015-02-03')

    @classmethod
    def make_loader(cls, events, columns):
        pass

    @classmethod
    def init_class_fixtures(cls):
        cls.sids = cls.events['sid'].unique()
        cls.columns = {
            Estimates.estimate: 'estimate',
            Estimates.event_date: EVENT_DATE_FIELD_NAME,
            Estimates.fiscal_quarter: FISCAL_QUARTER_FIELD_NAME,
            Estimates.fiscal_year: FISCAL_YEAR_FIELD_NAME,
        }
        cls.loader = cls.make_loader(
            events=cls.events,
            columns=cls.columns
        )
        cls.ASSET_FINDER_EQUITY_SIDS = list(
            cls.events[SID_FIELD_NAME].unique()
        )
        cls.ASSET_FINDER_EQUITY_SYMBOLS = [
            's' + str(n) for n in cls.ASSET_FINDER_EQUITY_SIDS
        ]
        super(EstimateTestCase, cls).init_class_fixtures()

    def _test_wrong_num_quarters_passed(self):
        with self.assertRaises(ValueError):
            dataset = QuartersEstimates(-1)
            engine = SimplePipelineEngine(
                lambda x: self.loader,
                self.trading_days,
                self.asset_finder,
            )

            engine.run_pipeline(
                Pipeline({c.name: c.latest for c in dataset.columns}),
                start_date=self.trading_days[0],
                end_date=self.trading_days[-1],
            )


window_test_cases = [
    (window_len, start_idx, num_quarters_out) for
    (window_len, start_idx), num_quarters_out in
    itertools.product(
        [[5, pd.Timestamp('2015-01-09').tz_localize('utc')],
         [6, pd.Timestamp('2015-01-12').tz_localize('utc')],
         [11, pd.Timestamp('2015-01-20').tz_localize('utc')],
         [19, pd.Timestamp('2015-01-30').tz_localize('utc')],
         [26, pd.Timestamp('2015-02-10').tz_localize('utc')]],
        [1, 2, 3, 4])
]


class NextEstimateWindowsTestCase(EstimateTestCase):
    events = estimates_timeline
    START_DATE = pd.Timestamp('2014-12-31')
    END_DATE = pd.Timestamp('2015-02-15')

    @classmethod
    def make_loader(cls, events, columns):
        return NextQuartersEstimatesLoader(events, columns)

    @parameterized.expand(window_test_cases)
    def test_next_estimate_windows_at_quarter_boundaries(self,
                                                         window_len,
                                                         start_idx,
                                                         num_quarters_out):
        dataset = QuartersEstimates(num_quarters_out)

        class SomeFactor(CustomFactor):
            inputs = [dataset.estimate]
            window_length = window_len

            def compute(self, today, assets, out, *inputs):
                unique_inputs = np.unique(inputs).tolist()
                requested_quarter = None
                if (pd.Timestamp('2015-02-10').tz_localize('utc') >= today >=
                        pd.Timestamp('2015-01-05').tz_localize('utc')):
                    next_quarter = estimates_timeline[
                            estimates_timeline[EVENT_DATE_FIELD_NAME] >= today
                        ].min()[FISCAL_QUARTER_FIELD_NAME]
                    requested_quarter = next_quarter + num_quarters_out - 1

                # If we know something about the requested quarter, assert
                # that all our estimates in the window are about that quarter.
                if requested_quarter and requested_quarter <= 4:
                    assert np.equal(unique_inputs, requested_quarter).all()
                else:
                    # We don't have any information yet about the next quarter
                    # or about the requested quarter; in that case, all our
                    # estimates in the window should be NaN across time.
                    assert np.isnan(unique_inputs).all()

        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )
        engine.run_pipeline(
            Pipeline({'est': SomeFactor()}),
            start_date=start_idx,
            end_date=self.trading_days[-1],
        )


class PreviousEstimateWindowsTestCase(EstimateTestCase):
    events = estimates_timeline
    START_DATE = pd.Timestamp('2014-12-31')
    END_DATE = pd.Timestamp('2015-02-15')

    @classmethod
    def make_loader(cls, events, columns):
        return PreviousQuartersEstimatesLoader(events, columns)

    @parameterized.expand(window_test_cases)
    def test_previous_estimate_windows_at_quarter_boundaries(self,
                                                             window_len,
                                                             start_idx,
                                                             num_quarters_out):
        dataset = QuartersEstimates(num_quarters_out)

        class SomeFactor(CustomFactor):
            inputs = [dataset.estimate]
            window_length = window_len

            def compute(self, today, assets, out, *inputs):
                unique_inputs = np.unique(inputs).tolist()
                requested_quarter = None
                if today >= pd.Timestamp('2015-01-12').tz_localize('utc'):
                    previous_quarter = estimates_timeline[
                            estimates_timeline[EVENT_DATE_FIELD_NAME] <= today
                        ].max()[FISCAL_QUARTER_FIELD_NAME]
                    requested_quarter = (
                        previous_quarter - (num_quarters_out - 1)
                    )

                # If we know something about the requested quarter, assert
                # that all our estimates in the window are about that quarter.
                if requested_quarter and requested_quarter >= 0:
                    assert np.equal(unique_inputs, requested_quarter).all()
                else:
                    # We don't have any information yet about the previous
                    # quarter
                    # or about the requested quarter; in that case, all our
                    # estimates in the window should be NaN across time.
                    assert np.isnan(unique_inputs).all()

        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )
        engine.run_pipeline(
            Pipeline({'est': SomeFactor()}),
            start_date=start_idx,
            end_date=self.trading_days[-1],
        )

class NextEstimateTestCase(EstimateTestCase):
    events = gen_estimates()

    @classmethod
    def make_loader(cls, events, columns):
        return NextQuartersEstimatesLoader(events, columns)

    def test_next_estimates(self):
        """
        The goal of this test is to make sure that we select the right
        datapoint as our 'next' w.r.t each date.
        """
        dataset = QuartersEstimates(1)
        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )

        results = engine.run_pipeline(
            Pipeline({c.name: c.latest for c in dataset.columns}),
            start_date=self.trading_days[0],
            end_date=self.trading_days[-1],
        )
        for sid in self.sids:
            sid_estimates = results.xs(sid, level=1)
            ts_sorted_estimates = self.events[
                self.events[SID_FIELD_NAME] == sid
            ].sort(TS_FIELD_NAME)
            for i, date in enumerate(sid_estimates.index):
                comparable_date = date.tz_localize(None)
                # Filter out estimates we don't know about yet.
                ts_eligible_estimates = ts_sorted_estimates[
                    ts_sorted_estimates[TS_FIELD_NAME] <= comparable_date
                ]
                expected_estimate = pd.DataFrame()
                if not ts_eligible_estimates.empty:
                    q1_knowledge = ts_eligible_estimates[
                        ts_eligible_estimates[FISCAL_QUARTER_FIELD_NAME] == 1
                    ]
                    q2_knowledge = ts_eligible_estimates[
                        ts_eligible_estimates[FISCAL_QUARTER_FIELD_NAME] == 2
                    ]

                    # If our latest knowledge of q1 is that the release is
                    # happening on this simulation date or later, then that's
                    # the estimate we want to use.
                    if (not q1_knowledge.empty and
                        q1_knowledge.iloc[-1][EVENT_DATE_FIELD_NAME] >=
                            comparable_date):
                        expected_estimate = q1_knowledge.iloc[-1]
                    # If q1 has already happened or we don't know about it
                    # yet and our latest knowledge indicates that q2 hasn't
                    # happend yet, then that's the estimate we want to use.
                    elif (not q2_knowledge.empty and
                          q2_knowledge.iloc[-1][EVENT_DATE_FIELD_NAME] >=
                            comparable_date):
                        expected_estimate = q2_knowledge.iloc[-1]
                if not expected_estimate.empty:
                    for colname in sid_estimates.columns:
                        expected_value = expected_estimate[colname]
                        computed_value = sid_estimates.iloc[i][colname]
                        assert_equal(expected_value, computed_value)
                else:
                    assert sid_estimates.iloc[i].isnull().all()

    def test_wrong_num_quarters_passed(self):
        self._test_wrong_num_quarters_passed()


class BlazeNextEstimateLoaderTestCase(NextEstimateTestCase):
    """
    Run the same tests as EventsLoaderTestCase, but using a BlazeEventsLoader.
    """

    @classmethod
    def make_loader(cls, events, columns):
        return BlazeNextEstimatesLoader(
            bz.data(events),
            columns,
        )


class PreviousEstimateTestCase(EstimateTestCase):
    events = gen_estimates()

    @classmethod
    def make_loader(cls, events, columns):
        return PreviousQuartersEstimatesLoader(events, columns)

    def test_previous_estimates(self):
        """
        The goal of this test is to make sure that we select the right
        datapoint as our 'previous' w.r.t each date.
        """
        dataset = QuartersEstimates(1)
        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )

        results = engine.run_pipeline(
            Pipeline({c.name: c.latest for c in dataset.columns}),
            start_date=self.trading_days[0],
            end_date=self.trading_days[-1],
        )
        for sid in self.sids:
            sid_estimates = results.xs(sid, level=1)
            ts_sorted_estimates = self.events[
                self.events[SID_FIELD_NAME] == sid
            ].sort(TS_FIELD_NAME)
            for i, date in enumerate(sid_estimates.index):
                comparable_date = date.tz_localize(None)
                # Filter out estimates we don't know about yet.
                ts_eligible_estimates = ts_sorted_estimates[
                    ts_sorted_estimates[TS_FIELD_NAME] <= comparable_date
                ]
                expected_estimate = pd.DataFrame()
                if not ts_eligible_estimates.empty:
                    # Determine the last piece of information we know about
                    # for q1 and q2. This takes advantage of the fact that we
                    # only have 2 quarters in the test data.
                    q1_knowledge = ts_eligible_estimates[
                        ts_eligible_estimates[FISCAL_QUARTER_FIELD_NAME] == 1
                    ]
                    q2_knowledge = ts_eligible_estimates[
                        ts_eligible_estimates[FISCAL_QUARTER_FIELD_NAME] == 2
                    ]
                    # The expected estimate will be for q2 if the last thing
                    # we've seen is that the release date already happened.
                    # Otherwise, it'll be for q1, as long as the release date
                    # for q1 has already happened.
                    if (not q2_knowledge.empty and
                        q2_knowledge.iloc[-1][EVENT_DATE_FIELD_NAME] <=
                            comparable_date):
                        expected_estimate = q2_knowledge.iloc[-1]
                    elif (not q1_knowledge.empty and
                          q1_knowledge.iloc[-1][EVENT_DATE_FIELD_NAME] <=
                            comparable_date):
                        expected_estimate = q1_knowledge.iloc[-1]
                if not expected_estimate.empty:
                    for colname in sid_estimates.columns:
                        expected_value = expected_estimate[colname]
                        computed_value = sid_estimates.iloc[i][colname]
                        assert_equal(expected_value, computed_value)
                else:
                    assert sid_estimates.iloc[i].isnull().all()

    def test_wrong_num_quarters_passed(self):
        self._test_wrong_num_quarters_passed()


class BlazePreviousEstimateLoaderTestCase(PreviousEstimateTestCase):
    """
    Run the same tests as EventsLoaderTestCase, but using a BlazeEventsLoader.
    """

    @classmethod
    def make_loader(cls, events, columns):
        return BlazePreviousEstimatesLoader(
            bz.data(events),
            columns,
        )


class QuarterShiftTestCase(ZiplineTestCase):
    """
    This tests, in isolation, quarter calculation logic for shifting quarters
    backwards/forwards from a starting point.
    """
    def test_quarter_normalization(self):
        input_yrs = pd.Series([0] * 4)
        input_qtrs = pd.Series(range(1, 5))
        result_years, result_quarters = split_normalized_quarters(
            normalize_quarters(input_yrs, input_qtrs)
        )
        # Can't use assert_series_equal here with check_names=False
        # because that still fails due to name differences.
        assert input_yrs.equals(result_years)
        assert input_qtrs.equals(result_quarters)
