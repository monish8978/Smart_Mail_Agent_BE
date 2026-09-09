def decision_engine(score: int, threshold: int = 80) -> str:
    """
    Decision engine based on reply quality score.

    threshold comes from email_accounts.score_threshold (per account).
    Defaults to 80 if not provided.

    - score >= threshold : auto_send
    - score <  threshold : create_ticket
    """
    if score >= threshold:
        return "auto_send"
    else:
        return "create_ticket"