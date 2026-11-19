from app.services.gap_assessment_report import omit_low_from_download
from app.services.report_train_agent import data_plan, learn_lines, trained_procedure


def test_report_train_learns_procedure_and_data_pull_without_case_facts():
    lines = [
        "Cloud Storage Service Usage",
        "Objective:",
        "To identify access to cloud storage.",
        "Procedure:",
        "The system was examined to identify access to cloud storage services. Browser activity was reviewed.",
        "Observation:",
        "User ada@example.com visited https://drive.google.com on 12/01/2024.",
    ]
    learned = learn_lines(lines, kind="disk")
    assert len(learned) == 1
    row = learned[0]
    assert row["title"] == "Cloud Storage Service Usage"
    assert "ada@example.com" not in row["procedure"]
    assert "drive.google.com" not in row["procedure"]
    assert "2024" not in row["procedure"]
    assert row["data_pulls"][0]["id"] == "cloud_storage"
    assert row["data_pulls"][0]["sources"] == ["browser_history"]
    assert "online file-storage page" in " ".join(row["prompt"]["questions"]).lower()
    assert "everyday words" in row["prompt"]["instruction"]
    assert "phone" in row["simple_procedure"].lower() or "web history" in row["simple_procedure"].lower()


def test_trained_procedure_is_used_for_a_matching_title(tmp_path, monkeypatch):
    from app.services import report_train_agent as agent

    model = {
        "objectives": [
            {
                "title": "USB and External Device Usage",
                "kind": "disk",
                "procedure": "The system was examined to identify connections of external devices.",
                "data_pulls": [{"id": "external_devices", "what": "Connected USB records", "how": "Read connected-device artifacts.", "sources": ["usb_devices"]}],
            }
        ]
    }
    monkeypatch.setattr(agent, "load_report_train_model", lambda: model)
    assert trained_procedure("USB and External Device Usage").startswith("The system was examined")
    plan = data_plan("USB and External Device Usage")
    assert plan["data_pulls"][0]["sources"] == ["usb_devices"]
    assert plan["mobile_device_fields"][1]["field"] == "Model"


def test_plain_observation_uses_saved_records_and_the_case_form(monkeypatch):
    from app.services import report_train_agent as agent

    monkeypatch.setattr(
        agent,
        "load_report_train_model",
        lambda: {
            "writing_style": "Use short everyday words.",
            "objectives": [
                {
                    "title": "Cloud Storage Service Usage",
                    "simple_procedure": "Look at the saved web history.",
                    "data_pulls": [
                        {
                            "what": "Web pages for online file storage that were opened",
                            "means_if_found": "an online file-storage page was opened. That does not show a file was sent or saved.",
                            "means_if_none": "the saved web history does not show a visit.",
                        }
                    ],
                    "prompt": {"questions": ["Did a storage page open?"], "instruction": "Use short everyday words."},
                }
            ],
        },
    )
    text = agent.plain_observation(
        "Cloud Storage Service Usage",
        {
            "objective": {"title": "Cloud Storage Service Usage"},
            "case_fact": {
                "status": "CONFIRMED",
                "key_entities": {"service": ["online storage page"]},
                "safe_evidence": ["A storage page was opened"],
            },
        },
        {"incident_summary": "Please check whether online storage was opened."},
    )
    assert "This means" in text
    assert "online storage page" in text
    assert "case form says" in text
    assert "Galaxy" not in text
    assert "build.prop" not in text


def test_a_new_sample_report_retrains_the_model(tmp_path):
    from docx import Document

    from app.services.report_train_agent import ensure_trained

    disk = tmp_path / "disk"
    mobile = tmp_path / "mobile"
    disk.mkdir()
    mobile.mkdir()

    def write_report(path, title):
        document = Document()
        document.add_paragraph(title)
        document.add_paragraph("Objective:")
        document.add_paragraph("To review cloud storage.")
        document.add_paragraph("Procedure:")
        document.add_paragraph("The system was examined to identify access to cloud storage services. Browser activity was reviewed.")
        document.add_paragraph("Observation:")
        document.add_paragraph("Nothing from the sample case is kept.")
        document.save(path)

    write_report(disk / "one.docx", "Cloud Storage Service Usage")
    first = ensure_trained(tmp_path)
    assert len(first["objectives"]) == 1
    assert first["prompt_version"] == 2

    write_report(disk / "two.docx", "USB and External Device Usage")
    second = ensure_trained(tmp_path)
    assert len(second["objectives"]) == 2
    assert second["sample_signature"] != first["sample_signature"]


def test_pdf_and_word_gap_downloads_omit_low_findings():
    inventory = omit_low_from_download(
        {
            "findings": [
                {"name": "Open SSH", "risk": "High"},
                {"name": "Old banner", "risk": "Low"},
                {"name": "Weak TLS", "risk": "Medium"},
            ],
            "informational_findings": [{"name": "Traceroute"}],
            "finding_summary": {
                "Critical": 0,
                "High": 1,
                "Medium": 1,
                "Low": 4,
                "Info": 2,
                "total": 8,
                "actionable": 6,
                "informational": 2,
            },
        }
    )
    assert [row["name"] for row in inventory["findings"]] == ["Open SSH", "Weak TLS"]
    assert inventory["finding_summary"]["Low"] == 0
    assert inventory["finding_summary"]["total"] == 4
    assert inventory["finding_summary"]["actionable"] == 2
    assert inventory["informational_findings"][0]["name"] == "Traceroute"
