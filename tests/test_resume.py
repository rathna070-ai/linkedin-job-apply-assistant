from jobapply.resume import extract_docx_text, extract_pdf_text, render_docx, render_pdf, safe_filename


def test_render_docx_round_trip(tailored):
    text = extract_docx_text(render_docx(tailored))
    assert "ASHA RAO" in text
    assert "- Owned delivery for a 40-member team." in text
    assert "Apr 2022 – Present" in text


def test_render_pdf_handles_special_characters(tailored):
    data = render_pdf(tailored)
    assert data.startswith(b"%PDF")
    text = extract_pdf_text(data)
    assert "ASHA RAO" in text
    assert "<weekly>" in text  # markup characters are escaped, not swallowed
    assert "->" in text  # unsupported arrow replaced


def test_safe_filename():
    assert safe_filename("Asha Rao", "Harp India", "Delivery Manager - BFSI") == "Asha_Rao_Harp_India_Delivery_Manager_BFSI"
