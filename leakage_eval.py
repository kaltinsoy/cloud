"""
Leakage-aware evaluation of the noisy-neighbor detector.
Compares RANDOM row split (potentially leaky) vs GROUP split by experiment
(victim, attacker, run_id) — the rigorous, leakage-free protocol for the paper.

Data: master_dataset.csv (86,720 rows, 160 experiments).
Labeling: identical multi-signal rule used in train_model.py v2.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split, GroupShuffleSplit, GroupKFold
from sklearn.metrics import f1_score, precision_score, recall_score, confusion_matrix

import os
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "master_dataset.csv")
FEATURES = ['IPC', 'Total_MB_per_sec', 'cache_dominance_score', 'LLC-load-misses', 'LLC-loads']
RNG = 42

print("[*] Loading", DATA)
df = pd.read_csv(DATA)
print(f"    rows={len(df):,}  cols={len(df.columns)}")

# --- Multi-signal labeling (same as train_model.py v2) ---
baseline = df[df['attacker'] == 'none'].groupby('victim')['IPC'].median()
df['baseline'] = df['victim'].map(baseline)
ipc_drop = (df['baseline'] - df['IPC']) / df['baseline']
df['label'] = (
    (df['attacker'] != 'none') &
    ((ipc_drop > 0.05) | (df['Total_MB_per_sec'] > 500) | (df['LLC-load-misses'] > 20000))
).astype(int)

# --- Experiment group id (one of the 160 runs) ---
df['exp'] = df['victim'].astype(str) + '|' + df['attacker'].astype(str) + '|' + df['run_id'].astype(str)

X = df[FEATURES].values
y = df['label'].values
groups = df['exp'].values

n_exp = df['exp'].nunique()
pos = int(y.sum()); neg = int(len(y) - pos)
print(f"[*] experiments(groups)={n_exp}")
print(f"[*] class balance: normal(0)={neg:,} ({neg/len(y)*100:.1f}%)  interference(1)={pos:,} ({pos/len(y)*100:.1f}%)")

def make_rf():
    return RandomForestClassifier(n_estimators=100, max_depth=12,
                                  class_weight='balanced', random_state=RNG, n_jobs=-1)

def report(tag, y_te, pred):
    f1 = f1_score(y_te, pred); pr = precision_score(y_te, pred); rc = recall_score(y_te, pred)
    cm = confusion_matrix(y_te, pred)
    print(f"\n=== {tag} ===")
    print(f"    F1={f1:.4f}  Precision={pr:.4f}  Recall={rc:.4f}")
    print(f"    Confusion [[TN FP],[FN TP]] = {cm.tolist()}")
    return f1

# 1) RANDOM ROW SPLIT (what we had — potentially leaky)
Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=RNG, stratify=y)
m = make_rf(); m.fit(Xtr, ytr)
f1_rand = report("RANDOM row split (80/20)  [LEAKY baseline]", yte, m.predict(Xte))

# 2) GROUP SPLIT BY EXPERIMENT (leakage-free, single 80/20)
gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=RNG)
tr_idx, te_idx = next(gss.split(X, y, groups))
m2 = make_rf(); m2.fit(X[tr_idx], y[tr_idx])
f1_grp = report("GROUP split by experiment (80/20)  [LEAKAGE-AWARE]", y[te_idx], m2.predict(X[te_idx]))
print(f"    train experiments={pd.Series(groups[tr_idx]).nunique()}  test experiments={pd.Series(groups[te_idx]).nunique()}")

# 3) GROUP K-FOLD CV (leakage-free, 5-fold by experiment)
gkf = GroupKFold(n_splits=5)
fold_f1 = []
for k,(tri,tei) in enumerate(gkf.split(X, y, groups), 1):
    mm = make_rf(); mm.fit(X[tri], y[tri])
    fold_f1.append(f1_score(y[tei], mm.predict(X[tei])))
fold_f1 = np.array(fold_f1)
print(f"\n=== GROUP 5-fold CV (leakage-aware) ===")
print(f"    fold F1s = {[f'{x:.4f}' for x in fold_f1]}")
print(f"    mean F1 = {fold_f1.mean():.4f} +/- {fold_f1.std():.4f}")

# Feature importance (from group model)
imp = sorted(zip(FEATURES, m2.feature_importances_), key=lambda z:-z[1])
print("\n=== Feature importance (group model) ===")
for f,i in imp: print(f"    {f:24s} {i*100:5.1f}%")

print("\n" + "="*60)
print("SUMMARY")
print(f"  Random-split F1 (leaky)     : {f1_rand:.4f}")
print(f"  Group-split F1 (leakage-aware): {f1_grp:.4f}")
print(f"  Group 5-fold CV F1          : {fold_f1.mean():.4f} +/- {fold_f1.std():.4f}")
print("="*60)
