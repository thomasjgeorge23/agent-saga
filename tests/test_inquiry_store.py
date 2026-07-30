import os
from pathlib import Path
from agent_saga.inquiry_store import record_inquiry, load_all_inquiries, format_inquiries_summary


def test_physical_inquiry_recording_and_retrieval(tmp_path, monkeypatch):
    # Redirect workspace files for isolated test
    test_file = tmp_path / "inquiries.json"
    test_backup = tmp_path / ".saga_inquiries.json"
    monkeypatch.setattr("agent_saga.inquiry_store.INQUIRIES_FILE", test_file)
    monkeypatch.setattr("agent_saga.inquiry_store.BACKUP_FILE", test_backup)

    rec = record_inquiry(
        name="Alice Enterprise",
        email="alice@corp.com",
        company="Corp AI",
        subject="Enterprise Integration",
        message="Great runtime safety system!",
    )

    assert rec["name"] == "Alice Enterprise"
    assert rec["email"] == "alice@corp.com"
    assert test_file.exists()
    assert test_backup.exists()

    all_records = load_all_inquiries()
    assert len(all_records) == 1
    assert all_records[0]["company"] == "Corp AI"

    summary = format_inquiries_summary()
    assert "Alice Enterprise" in summary
    assert "Corp AI" in summary
