from pathlib import Path

from dvd_fieldfix.gui import QueueItem, process_button_text


def test_process_button_label_reflects_selected_analysis_state() -> None:
    analyzed = QueueItem(Path("analyzed.mkv"))
    analyzed.analysis = object()  # type: ignore[assignment]
    pending = QueueItem(Path("pending.mkv"))

    assert process_button_text([]) == "Analyze + Process"
    assert process_button_text([pending]) == "Analyze + Process"
    assert process_button_text([analyzed, pending]) == "Analyze + Process"
    assert process_button_text([analyzed]) == "Process"
