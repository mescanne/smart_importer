"""Machine learning importer decorators."""

# pylint: disable=unsubscriptable-object

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from beancount.core import data
from beancount.core.data import (
    Close,
    Open,
    Transaction,
    filter_txns,
)
from beancount.core.data import sorted as beancount_sorted
from sklearn.pipeline import FeatureUnion, make_pipeline
from sklearn.svm import SVC

# Patch sk learn if dependency is installed
try:
    from sklearnex import patch_sklearn
    patch_sklearn()
except ImportError:
    pass

from smart_importer.entries import (
    merge_non_transaction_entries,
    set_entry_attribute,
)
from smart_importer.hooks import ImporterHook
from smart_importer.pipelines import get_pipeline

if TYPE_CHECKING:
    from beangulp import Importer
    from sklearn import Pipeline

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class EntryPredictor(ImporterHook):
    """Base class for machine learning importer helpers.

    Args:
        predict: Whether to add predictions to the entries.
        overwrite: When an attribute is predicted but already exists on an
            entry, overwrite the existing one.
        string_tokenizer: Tokenizer can let smart_importer support more
            languages. This parameter should be an callable function with
            string parameter and the returning should be a list.
        denylist_accounts: Transations with any of these accounts will be
            removed from the training data.
    """

    # pylint: disable=too-many-instance-attributes

    weights: dict[str, float] = {}
    attribute: str | None = None

    def __init__(
        self,
        predict: bool = True,
        overwrite: bool = False,
        string_tokenizer: Callable[[str], list] | None = None,
        denylist_accounts: list[str] | None = None,
        anchor_accounts: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.training_data = None
        self.open_accounts: dict[str, str] = {}
        self.denylist_accounts = set(denylist_accounts or [])
        self.pipeline: Pipeline | None = None
        self.is_fitted = False
        self.lock = threading.Lock()
        self.account: str | None = None
        self.anchor_accounts = anchor_accounts or []

        self.predict = predict
        self.overwrite = overwrite
        self.string_tokenizer = string_tokenizer

    def __call__(
        self,
        importer: Importer,
        file: str,
        imported_entries: data.Directives,
        existing_entries: data.Directives,
    ) -> data.Directives:
        """Predict attributes for imported transactions.

        Args:
            imported_entries: The list of imported entries.
            existing_entries: The list of existing entries as passed to the
                importer - will be used as training data.

        Returns:
            A list of entries, modified by this predictor.
        """
        logging.debug("Running %s for file %s", self.__class__.__name__, file)
        self.account = importer.account(file)
        self.load_training_data(existing_entries)
        with self.lock:
            self.define_pipeline()
            self.train_pipeline()
            return self.process_entries(imported_entries)

    def load_open_accounts(self, existing_entries):
        """Return map of accounts which have been opened but not closed."""
        account_map = {}

        for entry in beancount_sorted(existing_entries):
            # pylint: disable=isinstance-second-argument-not-valid-type
            if isinstance(entry, Open):
                account_map[entry.account] = entry
            elif isinstance(entry, Close):
                account_map.pop(entry.account)

        self.open_accounts = account_map

    def load_training_data(self, existing_entries):
        """Load training data, i.e., a list of Beancount entries."""
        all_entries = existing_entries or []
        self.load_open_accounts(all_entries)
        all_transactions = list(filter_txns(all_entries))
        self.training_data = [
            txn for txn in all_transactions if self.training_data_filter(txn)
        ]
        if not self.training_data:
            if len(all_transactions) > 0:
                logger.warning(
                    "Cannot train the machine learning model; "
                    "none of the training data matches the accounts"
                )
            else:
                logger.warning(
                    "Cannot train the machine learning model; "
                    "no training data found"
                )
        else:
            logger.info(
                "Loaded training data with %d transactions for account %s, "
                "filtered from %d total transactions",
                len(self.training_data),
                self.account,
                len(all_transactions),
            )

    def training_data_filter(self, txn):
        """Filter function for the training data."""
        found_import_account = False
        for pos in txn.postings:
            if pos.account not in self.open_accounts:
                return False
            if pos.account in self.denylist_accounts:
                return False
            if self.account == pos.account:
                found_import_account = True
            if pos.account in self.anchor_accounts:
                found_import_account = True

        return found_import_account or (not self.account and not self.anchor_accounts)

    @property
    def targets(self):
        """The training targets for the given training data.

        Returns:
            A list training targets (of the same length as the training data).
        """
        if not self.attribute:
            raise NotImplementedError
        return [
            getattr(entry, self.attribute) or ""
            for entry in self.training_data
        ]

    def define_pipeline(self) -> None:
        """Defines the machine learning pipeline based on given weights."""

        transformers = [
            (attribute, get_pipeline(attribute, self.string_tokenizer))
            for attribute in self.weights
        ]

        self.pipeline = make_pipeline(
            FeatureUnion(
                transformer_list=transformers, transformer_weights=self.weights
            ),
            SVC(kernel="linear"),
        )

    def train_pipeline(self) -> None:
        """Train the machine learning pipeline."""

        self.is_fitted = False
        targets_count = len(set(self.targets))

        if targets_count == 0:
            logger.warning(
                "Cannot train the machine learning model "
                "because there are no targets."
            )
        elif targets_count == 1:
            self.is_fitted = True
            logger.debug("Only one target possible.")
        else:
            assert self.pipeline is not None
            self.pipeline.fit(self.training_data, self.targets)
            self.is_fitted = True
            logger.info("Trained the machine learning model.")

    def process_entries(
        self, imported_entries: data.Directives
    ) -> data.Directives:
        """Process imported entries.

        Transactions might be modified, all other entries are left as is.

        Returns:
            The list of entries to be imported.
        """
        enhanced_transactions = self.process_transactions(
            list(filter_txns(imported_entries))
        )
        return merge_non_transaction_entries(
            imported_entries, enhanced_transactions
        )

    def apply_prediction(
        self, entry: data.Transaction, prediction: Any
    ) -> data.Transaction:
        """Apply a single prediction to an entry.

        Args:
            entry: A Beancount entry.
            prediction: The prediction for an attribute.

        Returns:
            The entry with the prediction applied.
        """
        if not self.attribute:
            raise NotImplementedError
        return set_entry_attribute(
            entry, self.attribute, prediction, overwrite=self.overwrite
        )

    def process_transactions(
        self, transactions: list[Transaction]
    ) -> list[Transaction]:
        """Process a list of transactions."""

        if not self.is_fitted or not transactions:
            return transactions

        if self.predict:
            if len(set(self.targets)) == 1:
                transactions = [
                    self.apply_prediction(entry, self.targets[0])
                    for entry in transactions
                ]
                logger.debug("Apply predictions without pipeline")
            elif self.pipeline:
                predictions = self.pipeline.predict(transactions)
                transactions = [
                    self.apply_prediction(entry, prediction)
                    for entry, prediction in zip(transactions, predictions)
                ]
                logger.debug("Apply predictions with pipeline")
            logger.debug(
                "Added predictions to %d transactions",
                len(transactions),
            )

        return transactions
