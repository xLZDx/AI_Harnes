import numpy as np
from sklearn.model_selection import KFold


def annualized_sharpe(daily_returns: np.ndarray) -> float:
    """Sharpe ratio of a 24/7 crypto strategy from daily returns."""
    mu, sd = daily_returns.mean(), daily_returns.std(ddof=1)
    return 0.0 if sd == 0 else float(mu / sd * np.sqrt(252))


def cross_validate(model, X: np.ndarray, y: np.ndarray, n_splits: int = 5) -> list[float]:
    """X, y are rows of hourly bars in time order; labels look 24 bars ahead."""
    scores = []
    for train_idx, test_idx in KFold(n_splits=n_splits, shuffle=True, random_state=7).split(X):
        model.fit(X[train_idx], y[train_idx])
        scores.append(model.score(X[test_idx], y[test_idx]))
    return scores


def position_size(equity: float, risk_fraction: float, entry: float, stop: float, contract_size: float) -> float:
    """Number of contracts so that hitting the stop loses risk_fraction of equity."""
    return equity * risk_fraction / abs(entry - stop)
