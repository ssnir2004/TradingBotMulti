"""Thin wrapper around ib_async used by trade_exec.py and executor.py."""
import time

from ib_async import IB, MarketOrder, Stock, Trade

SETTLED_STATUSES_TIMEOUT = 20


class IBKRClient:
    def __init__(self, host: str, port: int, client_id: int, account: str | None = None):
        self.ib = IB()
        self.ib.connect(host, port, clientId=client_id)
        # IBKR rejects every order with error 435 ("You must specify an
        # account") once a login is authorized for more than one account -
        # a single-account login auto-fills it and needs nothing here.
        # Stamped onto self.ib too so code that only has the raw ib_async
        # IB object can still read it.
        if account:
            self.account = account
        else:
            accounts = self.ib.managedAccounts()
            if len(accounts) > 1:
                raise RuntimeError(
                    f"This IBKR login manages multiple accounts {accounts} but no account "
                    "was configured for this user's Gateway connection."
                )
            self.account = accounts[0] if accounts else None
        self.ib.account = self.account

    def place_order(self, symbol: str, side: str, quantity: int) -> Trade:
        contract = Stock(symbol, "SMART", "USD")
        (qualified,) = self.ib.qualifyContracts(contract)

        order = MarketOrder(side, quantity)
        order.outsideRth = True
        if self.account:
            order.account = self.account
        # Market orders must be DAY (a market order can't stay open past the
        # session). Left unset, IBKR fills the TIF from the account's Order
        # Presets - on live that resolves to GTC, which is invalid for a
        # market order and gets the whole order cancelled (error 10349).
        order.tif = "DAY"
        trade = self.ib.placeOrder(qualified, order)

        deadline = time.monotonic() + SETTLED_STATUSES_TIMEOUT
        while time.monotonic() < deadline:
            self.ib.sleep(0.5)
            if trade.isDone():
                break

        return trade

    def disconnect(self):
        self.ib.disconnect()


def scoped_positions(ib: IB) -> list:
    """ib.positions() filtered to this connection's own resolved account
    (IBKRClient stamps it as ib.account). Unfiltered, a login authorized
    for more than one account returns every managed account's holdings
    mixed together."""
    return ib.positions(getattr(ib, "account", "") or "")


def belongs_to_account(ib: IB, acct_number: str | None) -> bool:
    """Whether an execution/order's own account attribution matches this
    connection's resolved account. True by default when this connection's
    account is unknown (single-account login) or the checked value is
    unexpectedly empty, so this only ever narrows results."""
    account = getattr(ib, "account", None)
    if not account or not acct_number:
        return True
    return acct_number == account
