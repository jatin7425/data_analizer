import pandas as pd

def analyze_excel_file(file_path):
    """
    Reads an Excel file, analyzes missing values, duplicates, 
    data types, and proposes normalization steps.
    """
    df = pd.read_excel(file_path)
    
    analysis = {
        'total_rows': df.shape[0],
        'total_columns': df.shape[1],
        'columns': list(df.columns),
        'missing_values': df.isnull().sum().to_dict(),
        'duplicate_rows': int(df.duplicated().sum()),
        'dtypes': {col: str(dtype) for col, dtype in df.dtypes.items()},
        'normalization_suggestions': []
    }
    
    # 1. Normalization Suggestion: Column naming convention
    unclean_cols = [col for col in df.columns if ' ' in str(col) or not str(col).islower()]
    if unclean_cols:
        analysis['normalization_suggestions'].append(
            f"Convert column names to snake_case for standard database compliance."
        )

    # 2. Normalization Suggestion: Missing data strategy
    cols_with_nulls = [col for col, count in analysis['missing_values'].items() if count > 0]
    if cols_with_nulls:
        analysis['normalization_suggestions'].append(
            f"Columns containing missing values: {', '.join(cols_with_nulls)}. Consider imputation or row removal."
        )

    # 3. Normalization Suggestion: De-duplication
    if analysis['duplicate_rows'] > 0:
        analysis['normalization_suggestions'].append(
            f"Found {analysis['duplicate_rows']} duplicate row(s). De-duplication recommended."
        )

    return analysis