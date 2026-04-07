# CRITICAL: Set matplotlib backend BEFORE importing pyplot (for Docker/headless)
import matplotlib
matplotlib.use('Agg')

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import warnings
import json
import os
from pathlib import Path
from typing import Optional, Union, List

warnings.filterwarnings('ignore')

# ============================================================
# LAZY DEPENDENCY CHECKS (Import only when needed)
# ============================================================

def _check_sklearn():
    """Check sklearn availability and return importable components"""
    try:
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.linear_model import LinearRegression, Ridge, Lasso
        from sklearn.model_selection import train_test_split, cross_val_score
        from sklearn.preprocessing import StandardScaler, LabelEncoder
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics import r2_score, mean_squared_error, mean_absolute_error
        return {
            "available": True,
            "RandomForestRegressor": RandomForestRegressor,
            "LinearRegression": LinearRegression,
            "Ridge": Ridge,
            "Lasso": Lasso,
            "train_test_split": train_test_split,
            "cross_val_score": cross_val_score,
            "StandardScaler": StandardScaler,
            "LabelEncoder": LabelEncoder,
            "TfidfVectorizer": TfidfVectorizer,
            "r2_score": r2_score,
            "mean_squared_error": mean_squared_error,
            "mean_absolute_error": mean_absolute_error,
        }
    except ImportError as e:
        return {"available": False, "error": str(e)}

def _check_seaborn():
    """Check seaborn availability"""
    try:
        import seaborn as sns
        return {"available": True, "sns": sns}
    except ImportError:
        return {"available": False}

_SKLEARN = _check_sklearn()
_SEABORN = _check_seaborn()

# ============================================================
# CONSTANTS
# ============================================================

NAME = "data_science"
DOC = "Data Science: EDA, ML prediction (RandomForest/LinearRegression), NLP text analysis, visualization. Supports .csv, .xlsx, .json."

# ============================================================
# PATH HANDLING
# ============================================================

def _resolve_path(file_path: str) -> str:
    """Resolve file path - handles absolute, relative to /app/, or bare filenames"""
    if not file_path:
        raise ValueError("file_path cannot be empty")
    if os.path.isabs(file_path):
        return os.path.normpath(file_path)
    clean_path = file_path.lstrip('/').replace('app/', '', 1)
    return os.path.normpath(os.path.join('/app', clean_path))

# ============================================================
# DATA LOADING
# ============================================================

def _load_data(file_path: str) -> pd.DataFrame:
    """Load data with path resolution and format detection"""
    resolved_path = _resolve_path(file_path)
    
    if not os.path.exists(resolved_path):
        alt_path = _resolve_path(f"memory/{file_path}")
        if os.path.exists(alt_path):
            resolved_path = alt_path
        else:
            raise FileNotFoundError(
                f"File not found: '{file_path}'\n"
                f"Tried: {resolved_path}\n"
                f"Available in /app/: {os.listdir('/app') if os.path.exists('/app') else 'N/A'}"
            )
    
    ext = os.path.splitext(resolved_path)[1].lower()
    if ext == '.csv':
        return pd.read_csv(resolved_path)
    elif ext in ['.xlsx', '.xls']:
        return pd.read_excel(resolved_path)
    elif ext == '.json':
        return pd.read_json(resolved_path)
    else:
        raise ValueError(f"Unsupported format: '{ext}'. Supported: .csv, .xlsx, .xls, .json")

# ============================================================
# JSON SERIALIZATION
# ============================================================

def _safe_json_dump(obj) -> str:
    """Safely convert any object to JSON string, handling numpy/pandas types"""
    def convert(o):
        """Recursively convert numpy/pandas types to JSON-serializable Python primitives."""
        if isinstance(o, (np.integer, np.int64, np.int32)):
            return int(o)
        elif isinstance(o, (np.floating, np.float64, np.float32)):
            return None if np.isnan(o) else float(o)
        elif isinstance(o, np.ndarray):
            return o.tolist()
        elif isinstance(o, np.bool_):
            return bool(o)
        elif isinstance(o, pd.Series):
            return o.to_list()
        elif isinstance(o, pd.DataFrame):
            return o.to_dict(orient='records')
        elif pd.isna(o):
            return None
        elif isinstance(o, dict):
            return {str(k): convert(v) for k, v in o.items()}
        elif isinstance(o, (list, tuple)):
            return [convert(v) for v in o]
        elif isinstance(o, set):
            return list(o)
        elif hasattr(o, 'isoformat'):
            return o.isoformat()
        return str(o)
    
    try:
        return json.dumps(convert(obj), indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"JSON serialization failed: {str(e)}", "original_type": str(type(obj))})

# ============================================================
# PLOTTING HELPERS
# ============================================================

def _save_plot(plot_path: str, title: str = "") -> str:
    """Save plot with proper cleanup"""
    try:
        os.makedirs(os.path.dirname(plot_path), exist_ok=True)
        plt.tight_layout()
        plt.savefig(plot_path, dpi=150, bbox_inches='tight', facecolor='white')
        return f"✅ Plot saved: {plot_path}"
    except Exception as e:
        return f"❌ Failed to save plot: {e}"
    finally:
        plt.close('all')

# ============================================================
# SKILL FUNCTIONS
# ============================================================

def analyze_dataset(file_path: str) -> str:
    """Generate EDA report: shape, columns, missing values, dtypes, statistics"""
    try:
        df = _load_data(file_path)
        
        report = {
            'file': file_path,
            'shape': {'rows': int(df.shape[0]), 'columns': int(df.shape[1])},
            'columns': df.columns.tolist(),
            'column_types': {str(k): str(v) for k, v in df.dtypes.to_dict().items()},
            'missing_values': df.isnull().sum().to_dict(),
            'missing_percentage': {k: round(v/len(df)*100, 2) for k, v in df.isnull().sum().to_dict().items()},
            'sample_rows': df.head(3).to_dict(orient='records'),
        }
        
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        if numeric_cols:
            try:
                report['statistics'] = df[numeric_cols].describe().to_dict()
            except Exception as e:
                report['statistics'] = {'error': str(e)}
        else:
            report['statistics'] = {'note': 'No numeric columns for statistics'}
        
        categorical_cols = df.select_dtypes(include=['object', 'category']).columns.tolist()
        if categorical_cols:
            report['unique_counts'] = {col: int(df[col].nunique()) for col in categorical_cols[:10]}
        
        return _safe_json_dump(report)
        
    except FileNotFoundError as e:
        return f"❌ File Error: {e}"
    except Exception as e:
        return f"❌ Analysis Error: {type(e).__name__}: {e}"


def predict_column(
    file_path: str, 
    target_column: str = 'target',
    model_type: str = 'random_forest'  # NEW: 'random_forest' or 'linear'
) -> str:
    """
    Predict any numeric column using Random Forest or Linear Regression.
    
    Usage: 
    <skill:data_science.predict_column>/app/data/file.csv, target_column=sales, model_type=linear</skill:data_science.predict_column>
    """
    # Check sklearn availability FIRST
    if not _SKLEARN['available']:
        return f"❌ sklearn not installed or import failed: {_SKLEARN.get('error', 'Unknown error')}\nInstall: pip install scikit-learn"
    
    # Lazy import - only when we know sklearn is available
    try:
        RandomForestRegressor = _SKLEARN['RandomForestRegressor']
        LinearRegression = _SKLEARN['LinearRegression']
        train_test_split = _SKLEARN['train_test_split']
        StandardScaler = _SKLEARN['StandardScaler']
        r2_score = _SKLEARN['r2_score']
        mean_squared_error = _SKLEARN['mean_squared_error']
    except KeyError as e:
        return f"❌ Internal error: missing sklearn component {e}"
    
    try:
        df = _load_data(file_path)
        
        # Validate target column
        if target_column not in df.columns:
            available = ", ".join(list(df.columns)[:10])
            return f"❌ Column '{target_column}' not found. Available: {available}..."
        
        # Check if target is numeric
        if not pd.api.types.is_numeric_dtype(df[target_column]):
            return f"❌ Column '{target_column}' must be numeric for prediction. Type: {df[target_column].dtype}"
        
        # Data cleaning
        initial_rows = len(df)
        df = df.dropna(subset=[target_column]).copy()
        dropped_rows = initial_rows - len(df)
        
        # Fill numeric NAs with median
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        for col in numeric_cols:
            if col != target_column:
                df[col] = df[col].fillna(df[col].median())
        
        # Separate features and target
        X = df.drop(columns=[target_column])
        y = df[target_column]
        
        # Convert categoricals to dummy variables
        X = pd.get_dummies(X, drop_first=True, dtype=float)
        
        # Validate features
        if X.empty:
            return "❌ No usable features after processing. Need at least one numeric or categorical column besides target."
        
        # Check minimum samples
        if len(X) < 10:
            return f"❌ Not enough data: only {len(X)} rows (need at least 10)"
        
        # Train/test split
        test_size = min(0.25, max(0.1, 5/len(X)))
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=test_size, random_state=42)
        
        # Scale features for linear models (optional but recommended)
        if model_type == 'linear':
            scaler = StandardScaler()
            X_train_scaled = scaler.fit_transform(X_train)
            X_test_scaled = scaler.transform(X_test)
        else:
            X_train_scaled = X_train
            X_test_scaled = X_test
        
        # Train model based on type
        if model_type == 'linear':
            model = LinearRegression()
            model_name = "LinearRegression"
        elif model_type == 'ridge':
            Ridge = _SKLEARN.get('Ridge')
            if not Ridge:
                return "❌ Ridge regression not available in this sklearn version"
            model = Ridge(alpha=1.0)
            model_name = "Ridge"
        elif model_type == 'lasso':
            Lasso = _SKLEARN.get('Lasso')
            if not Lasso:
                return "❌ Lasso regression not available in this sklearn version"
            model = Lasso(alpha=0.1)
            model_name = "Lasso"
        else:  # random_forest (default)
            n_estimators = min(100, max(10, len(X) // 10))
            model = RandomForestRegressor(n_estimators=n_estimators, random_state=42, n_jobs=-1)
            model_name = f"RandomForestRegressor(n={n_estimators})"
        
        model.fit(X_train_scaled, y_train)
        
        # Predictions and metrics
        predictions = model.predict(X_test_scaled)
        r2 = r2_score(y_test, predictions)
        rmse = float(np.sqrt(mean_squared_error(y_test, predictions)))
        mae = float(_SKLEARN.get('mean_absolute_error', lambda a,b: 0)(y_test, predictions))
        
        # Feature importance (only for tree-based models)
        top_features = {}
        if hasattr(model, 'feature_importances_'):
            importance = dict(zip(X.columns, model.feature_importances_))
            top_features = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True)[:5])
        elif hasattr(model, 'coef_') and model_type == 'linear':
            # Linear model coefficients
            coef = dict(zip(X.columns, model.coef_))
            top_features = dict(sorted(coef.items(), key=lambda x: abs(x[1]), reverse=True)[:5])
        
        # Create visualization
        plt.figure(figsize=(10, 6))
        plt.scatter(y_test, predictions, alpha=0.6, edgecolors='k', s=30)
        min_val = min(y_test.min(), predictions.min())
        max_val = max(y_test.max(), predictions.max())
        plt.plot([min_val, max_val], [min_val, max_val], 'r--', lw=2, label='Perfect prediction')
        plt.xlabel('Actual Values')
        plt.ylabel('Predicted Values')
        plt.title(f'Prediction Results: {target_column} (R²={r2:.3f})')
        plt.legend()
        plt.grid(True, alpha=0.3)
        
        plot_path = '/app/memory/prediction_results.png'
        plot_result = _save_plot(plot_path, f'Prediction: {target_column}')
        
        result = {
            'success': True,
            'target_column': target_column,
            'model_used': model_name,
            'samples_used': len(X),
            'rows_dropped_for_missing': dropped_rows,
            'features_used': len(X.columns),
            'r2_score': round(r2, 4),
            'rmse': round(rmse, 4),
            'mae': round(mae, 4),
            'plot_saved': plot_result,
            'top_5_features': top_features,
            'model_config': {
                'algorithm': model_name,
                'test_size': round(test_size, 2),
                'scaled': model_type == 'linear'
            }
        }
        
        return _safe_json_dump(result)
        
    except FileNotFoundError as e:
        return f"❌ File Error: {e}"
    except Exception as e:
        return f"❌ Prediction Error: {type(e).__name__}: {e}"


def analyze_text_column(
    file_path: str, 
    text_column: str,
    max_features: int = 100,
    top_n_words: int = 20
) -> str:
    """
    Analyze text data using TF-IDF vectorization.
    NEW: Properly initializes TfidfVectorizer before fit_transform.
    
    Usage: <skill:data_science.analyze_text_column>/app/data/reviews.csv, text_column=review</skill:data_science.analyze_text_column>
    """
    # Check sklearn availability
    if not _SKLEARN['available']:
        return f"❌ sklearn not installed: {_SKLEARN.get('error', 'Unknown error')}"
    
    try:
        TfidfVectorizer = _SKLEARN['TfidfVectorizer']
    except KeyError:
        return "❌ TfidfVectorizer not available in this sklearn version"
    
    try:
        df = _load_data(file_path)
        
        if text_column not in df.columns:
            return f"❌ Column '{text_column}' not found. Available: {list(df.columns)[:10]}..."
        
        # Extract and clean text
        texts = df[text_column].dropna().astype(str).tolist()
        if not texts:
            return "❌ No valid text data found in column"
        
        # ✅ PROPERLY INITIALIZE TfidfVectorizer BEFORE fit_transform
        vectorizer = TfidfVectorizer(
            max_features=max_features,
            stop_words='english',
            ngram_range=(1, 2),
            min_df=2,
            max_df=0.8
        )
        
        # Now fit_transform (this was the bug - calling without initialization)
        tfidf_matrix = vectorizer.fit_transform(texts)
        
        # Get feature names and top terms
        feature_names = vectorizer.get_feature_names_out()
        
        # Calculate average TF-IDF scores per term
        mean_tfidf = np.asarray(tfidf_matrix.mean(axis=0)).flatten()
        top_terms_idx = mean_tfidf.argsort()[-top_n_words:][::-1]
        top_terms = [(feature_names[i], round(mean_tfidf[i], 4)) for i in top_terms_idx]
        
        result = {
            'success': True,
            'column': text_column,
            'documents_processed': len(texts),
            'vocabulary_size': len(feature_names),
            'matrix_shape': tfidf_matrix.shape,
            'top_terms': top_terms,
            'vectorizer_config': {
                'max_features': max_features,
                'ngram_range': '(1, 2)',
                'min_df': 2,
                'max_df': 0.8
            }
        }
        
        return _safe_json_dump(result)
        
    except FileNotFoundError as e:
        return f"❌ File Error: {e}"
    except Exception as e:
        return f"❌ Text Analysis Error: {type(e).__name__}: {e}"


def create_chart(
    file_path: str, 
    chart_type: str = 'bar', 
    x_column: Optional[str] = None, 
    y_column: Optional[str] = None,
    title: Optional[str] = None
) -> str:
    """Create visualization charts (unchanged from working version)"""
    try:
        df = _load_data(file_path)
        
        if x_column and x_column not in df.columns:
            return f"❌ x_column '{x_column}' not found. Available: {list(df.columns)[:10]}..."
        if y_column and y_column not in df.columns:
            return f"❌ y_column '{y_column}' not found. Available: {list(df.columns)[:10]}..."
        
        plt.figure(figsize=(12, 6))
        filename = os.path.basename(file_path)
        chart_title = title or f'{chart_type.title()} Chart: {filename}'
        
        if chart_type == 'bar':
            if x_column and y_column:
                agg = df.groupby(x_column)[y_column].sum().reset_index()
                plt.bar(agg[x_column], agg[y_column])
                plt.xticks(rotation=45, ha='right')
            elif x_column:
                df[x_column].value_counts().head(20).plot(kind='bar')
                plt.xticks(rotation=45, ha='right')
            elif y_column:
                df[y_column].value_counts().head(20).plot(kind='bar')
            else:
                return "❌ Bar chart requires at least x_column or y_column"
            plt.xlabel(x_column or 'Category')
            plt.ylabel(y_column or 'Count')
            
        elif chart_type == 'line':
            if x_column and y_column:
                plt.plot(df[x_column], df[y_column], marker='o', linewidth=2)
                plt.xlabel(x_column)
                plt.ylabel(y_column)
                plt.xticks(rotation=45, ha='right')
            elif y_column:
                df[y_column].plot(kind='line', marker='o')
                plt.ylabel(y_column)
            else:
                df.plot(kind='line', marker='o')
            plt.grid(True, alpha=0.3)
            
        elif chart_type == 'scatter':
            if x_column and y_column:
                plt.scatter(df[x_column], df[y_column], alpha=0.6, s=30)
                plt.xlabel(x_column)
                plt.ylabel(y_column)
            else:
                return "❌ Scatter plot requires both x_column and y_column"
                
        elif chart_type == 'histogram':
            col = y_column or (df.select_dtypes(include=[np.number]).columns[0] if len(df.select_dtypes(include=[np.number]).columns) > 0 else None)
            if col and col in df.columns:
                plt.hist(df[col].dropna(), bins=30, edgecolor='black', alpha=0.7)
                plt.xlabel(col)
                plt.ylabel('Frequency')
            else:
                return "❌ Histogram requires a numeric y_column"
                
        elif chart_type == 'heatmap':
            if not _SEABORN['available']:
                return "❌ Heatmap requires seaborn. Install: pip install seaborn"
            sns = _SEABORN['sns']
            numeric_df = df.select_dtypes(include=[np.number])
            if numeric_df.empty or len(numeric_df.columns) < 2:
                return "❌ Need at least 2 numeric columns for correlation heatmap"
            corr = numeric_df.corr()
            sns.heatmap(corr, annot=True, cmap='coolwarm', center=0, fmt='.2f', square=True)
            plt.title('Correlation Heatmap')
            
        elif chart_type == 'box':
            if y_column:
                plt.boxplot(df[y_column].dropna(), vert=True, patch_artist=True)
                plt.ylabel(y_column)
            else:
                numeric_cols = df.select_dtypes(include=[np.number]).columns[:5].tolist()
                if not numeric_cols:
                    return "❌ No numeric columns for box plot"
                df[numeric_cols].boxplot()
                
        elif chart_type == 'kde':
            col = y_column or (df.select_dtypes(include=[np.number]).columns[0] if len(df.select_dtypes(include=[np.number]).columns) > 0 else None)
            if col and col in df.columns:
                df[col].dropna().plot(kind='kde', fill=True)
                plt.xlabel(col)
                plt.ylabel('Density')
            else:
                return "❌ KDE plot requires a numeric y_column"
        else:
            return f"❌ Unknown chart type: '{chart_type}'. Use: bar, line, scatter, histogram, heatmap, box, kde"
        
        plt.title(chart_title)
        
        safe_filename = "".join(c for c in filename if c.isalnum() or c in '._-')[:50]
        plot_path = f'/app/memory/chart_{chart_type}_{safe_filename}.png'
        plot_result = _save_plot(plot_path, chart_title)
        
        return f"{plot_result}\nColumns: x={x_column}, y={y_column}, type={chart_type}"
        
    except FileNotFoundError as e:
        return f"❌ File Error: {e}"
    except Exception as e:
        return f"❌ Chart Error: {type(e).__name__}: {e}"


# ============================================================
# BACKWARDS COMPATIBILITY ALIASES
# ============================================================

def predict_sales(file_path: str, target_col: str = 'sales') -> str:
    """Alias for predict_column"""
    return predict_column(file_path, target_column=target_col)

def eda(file_path: str) -> str:
    """Alias for analyze_dataset"""
    return analyze_dataset(file_path)

def plot(file_path: str, chart_type: str = 'bar', x: str = None, y: str = None) -> str:
    """Alias for create_chart"""
    return create_chart(file_path, chart_type=chart_type, x_column=x, y_column=y)

def tfidf(file_path: str, text_column: str) -> str:
    """Alias for analyze_text_column"""
    return analyze_text_column(file_path, text_column=text_column)


# ============================================================
# STATUS / HEALTH CHECK
# ============================================================

def status() -> str:
    """Return skill status and available dependencies"""
    sklearn_status = "✅" if _SKLEARN['available'] else f"❌ ({_SKLEARN.get('error', 'import failed')})"
    seaborn_status = "✅" if _SEABORN['available'] else "⚠️ (optional)"
    
    info = [
        "📊 Data Science Skill Status",
        f"✅ pandas: True",
        f"✅ numpy: True", 
        f"✅ matplotlib: True",
        f"{seaborn_status} seaborn: {_SEABORN['available']} (for heatmap)",
        f"{sklearn_status} scikit-learn: {_SKLEARN['available']}",
        "",
        "Available functions:",
        "  • analyze_dataset(file_path) - EDA report",
        "  • predict_column(file_path, target_column, model_type) - ML (random_forest/linear)",
        "  • analyze_text_column(file_path, text_column) - NLP with TF-IDF",
        "  • create_chart(file_path, chart_type, x_column, y_column) - Visualization",
        "",
        "Supported formats: .csv, .xlsx, .xls, .json",
        "Output plots saved to: /app/memory/"
    ]
    return "\n".join(info)