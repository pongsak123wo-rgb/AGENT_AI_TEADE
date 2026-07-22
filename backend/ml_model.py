"""Machine Learning model to predict trade win probability.
Trains a lightweight Random Forest on historical signals to learn
what combinations of indicators actually work.
"""
from __future__ import annotations

import json
import sqlite3
import pandas as pd
from pathlib import Path
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.model_selection import StratifiedKFold, cross_val_score
import numpy as np

DB_PATH = Path(__file__).parent / "signals.db"

_model: Pipeline | None = None
_trained_features: list[str] = []
_trained_sample_count: int = 0
RETRAIN_EVERY_N_NEW_SAMPLES = 5

def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _count_closed_signals() -> int:
    conn = _connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM signals WHERE status IN ('win', 'loss')").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    conn.close()
    return n

def train_model() -> dict:
    global _model, _trained_features, _trained_sample_count
    conn = _connect()
    try:
        rows = conn.execute("SELECT status, action, indicators_json FROM signals WHERE status IN ('win', 'loss')").fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {"status": "no_data_column"}
    conn.close()

    if len(rows) < 10:
        return {"status": "not_enough_data", "samples": len(rows)}

    data = []
    labels = []
    
    for row in rows:
        try:
            indicators = json.loads(row["indicators_json"]) if row["indicators_json"] else {}
        except Exception:
            continue
            
        if not indicators:
            continue

        # Extract numeric features
        features = {}
        for k, v in indicators.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                features[k] = v
                
        # One-hot encode the action
        features["is_buy"] = 1.0 if row["action"] == "buy" else 0.0
        
        data.append(features)
        labels.append(1 if row["status"] == "win" else 0)

    if len(data) < 10:
        return {"status": "not_enough_valid_data"}

    df = pd.DataFrame(data)
    df.fillna(0, inplace=True) # basic imputation
    _trained_features = list(df.columns)
    
    X = df.values
    y = np.array(labels)

    pipeline = Pipeline([
        ('scaler', StandardScaler()),
        ('rf', RandomForestClassifier(n_estimators=50, max_depth=5, random_state=42))
    ])

    # Honest generalization estimate via k-fold cross-validation — fitting
    # and scoring on the SAME data (the old behavior) only measures how
    # well the model memorized the training set, not whether it works on
    # a trade it hasn't seen. With this little data, a held-out test split
    # would leave too few samples to train on, so CV is the practical
    # alternative. Needs at least 2 samples of each class per fold.
    class_counts = np.bincount(y)
    min_class_count = int(class_counts.min()) if len(class_counts) > 1 else 0
    cv_accuracy = None
    cv_folds = 0
    if min_class_count >= 2:
        cv_folds = min(5, min_class_count)
        try:
            cv_scores = cross_val_score(pipeline, X, y, cv=StratifiedKFold(n_splits=cv_folds, shuffle=True, random_state=42))
            cv_accuracy = round(float(cv_scores.mean()) * 100, 1)
        except ValueError:
            cv_accuracy = None

    # Fit the final model on ALL available data for actual predictions —
    # cross-validation above is only used to report an honest accuracy
    # estimate; the deployed model should still learn from every sample.
    pipeline.fit(X, y)
    _model = pipeline
    _trained_sample_count = len(rows)

    train_accuracy = round(float(pipeline.score(X, y)) * 100, 1)

    if cv_accuracy is not None:
        note = f"cv_accuracy คือค่าประมาณจริงจาก {cv_folds}-fold cross-validation (ทดสอบกับข้อมูลที่ไม่ได้ใช้เทรน) — ใช้ตัวนี้ตัดสิน ไม่ใช่ train_accuracy"
    else:
        note = "ข้อมูลยังน้อย/ไม่สมดุลเกินจะทำ cross-validation ที่น่าเชื่อถือ — train_accuracy ด้านบนนี้วัดกับข้อมูลที่มันเทรนเอง มักดูดีเกินจริง (overfit) ห้ามเชื่อเป็นตัวชี้วัดจริง"

    return {
        "status": "trained",
        "samples": len(X),
        "cv_accuracy": cv_accuracy,
        "cv_folds": cv_folds,
        "train_accuracy": train_accuracy,
        "note": note,
        "features": _trained_features,
    }

def predict_win_probability(action: str, indicators: dict) -> float | None:
    """Returns probability of win (0.0 to 100.0) or None if model not trained.
    Retrains every RETRAIN_EVERY_N_NEW_SAMPLES new closed signals so the
    model actually keeps learning instead of freezing on its first fit.
    """
    if _model is None or (_count_closed_signals() - _trained_sample_count) >= RETRAIN_EVERY_N_NEW_SAMPLES:
        train_model()

    if _model is None:
        return None

    features = {}
    for k, v in indicators.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            features[k] = v
    features["is_buy"] = 1.0 if action == "buy" else 0.0

    # Ensure feature vector matches training
    x_input = []
    for f in _trained_features:
        x_input.append(features.get(f, 0.0))
        
    X = np.array([x_input])
    probs = _model.predict_proba(X)
    
    # probs[0][1] is probability of class 1 (win)
    return round(probs[0][1] * 100, 1)

