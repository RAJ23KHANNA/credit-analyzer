import os
import tempfile
from datetime import date

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse

from bs4 import BeautifulSoup
from pathlib import Path

import extract_cibil
import populate_dashboard

import crif_extractor
import dashboard_populator


app = FastAPI(title="Credit Report Analyzer")
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB file size limit

# ---------------------------------------------------------
# Load the clean dashboard template once at startup
# ---------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "dashboard_template.html", "r", encoding="utf-8") as f:
    BASE_TEMPLATE = f.read()

with open(BASE_DIR / "dashboard_template_crif.html", "r", encoding="utf-8") as f:
    CRIF_BASE_TEMPLATE = f.read()

# ---------------------------------------------------------
# Landing page
# ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Credit Report Analyzer</title>

        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        <style>

            * {
                box-sizing: border-box;
            }

            body {
                font-family: Arial, sans-serif;
                background: #f5f7fa;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                margin: 0;
                padding: 30px;
            }

            .container {
                width: min(900px, 100%);
                text-align: center;
            }

            h1 {
                margin-bottom: 10px;
                color: #111827;
                font-size: 32px;
            }

            .subtitle {
                color: #6b7280;
                margin-bottom: 35px;
                font-size: 16px;
            }

            .cards {
                display: grid;
                grid-template-columns:
                    repeat(2, minmax(0, 1fr));

                gap: 25px;
            }

            .card {
                background: white;
                padding: 35px;
                border-radius: 18px;
                box-shadow:
                    0 10px 30px rgba(0,0,0,0.08);

                border: 1px solid #e5e7eb;
            }

            .card h2 {
                margin-top: 0;
                margin-bottom: 10px;
                color: #111827;
            }

            .card p {
                color: #6b7280;
                min-height: 45px;
                line-height: 1.5;
            }

            input {
                margin: 20px 0;
                width: 100%;
            }

            button {
                width: 100%;
                background: #0369a1;
                color: white;
                border: none;
                padding: 13px 20px;
                border-radius: 8px;
                cursor: pointer;
                font-size: 15px;
                font-weight: bold;
            }

            button:hover {
                background: #075985;
            }

            .crif button {
                background: #047857;
            }

            .crif button:hover {
                background: #065f46;
            }

            .badge {
                display: inline-block;
                padding: 6px 12px;
                border-radius: 999px;
                font-size: 12px;
                font-weight: bold;
                margin-bottom: 15px;
            }

            .cibil .badge {
                background: #e0f2fe;
                color: #0369a1;
            }

            .crif .badge {
                background: #d1fae5;
                color: #047857;
            }

            @media (max-width: 700px) {

                .cards {
                    grid-template-columns: 1fr;
                }

                h1 {
                    font-size: 26px;
                }

                body {
                    padding: 20px;
                }
            }

        </style>
    </head>

    <body>

        <div class="container">

            <h1>Credit Report Analyzer</h1>

            <div class="subtitle">
                Upload a credit bureau report to analyze
                the borrower's credit profile.
            </div>


            <div class="cards">


                <!-- CIBIL CARD -->

                <div class="card cibil">

                    <span class="badge">
                        TransUnion CIBIL
                    </span>

                    <h2>CIBIL Report</h2>

                    <p>
                        Analyze a TransUnion CIBIL
                        Consumer Credit Information Report.
                    </p>

                    <form
                        action="/analyze"
                        method="post"
                        enctype="multipart/form-data"
                    >

                        <input
                            type="file"
                            name="file"
                            accept=".pdf,application/pdf"
                            required
                        >

                        <button type="submit">
                            Analyze CIBIL Report
                        </button>

                    </form>

                </div>


                <!-- CRIF CARD -->

                <div class="card crif">

                    <span class="badge">
                        CRIF High Mark
                    </span>

                    <h2>CRIF Report</h2>

                    <p>
                        Analyze a CRIF High Mark
                        Consumer Credit Report.
                    </p>

                    <form
                        action="/analyze/crif"
                        method="post"
                        enctype="multipart/form-data"
                    >

                        <input
                            type="file"
                            name="file"
                            accept=".pdf,application/pdf"
                            required
                        >

                        <button type="submit">
                            Analyze CRIF Report
                        </button>

                    </form>

                </div>

            </div>

        </div>

    </body>
    </html>
    """

# ---------------------------------------------------------
# Analyze uploaded CIBIL PDF
# ---------------------------------------------------------

@app.post("/analyze", response_class=HTMLResponse)
async def analyze_cibil(file: UploadFile = File(...)):

    # -----------------------------------------------------
    # Basic file validation
    # -----------------------------------------------------

    if not file.filename:
        return HTMLResponse(
            "<h2>No file selected.</h2>",
            status_code=400
        )

    if not file.filename.lower().endswith(".pdf"):
        return HTMLResponse(
            "<h2>Please upload a PDF file.</h2>",
            status_code=400
        )

    content = await file.read()

    if len(content) > MAX_UPLOAD_SIZE:
        return HTMLResponse(
            "<h2>File is too large.</h2>"
            "<p>Please upload a PDF smaller than 10 MB.</p>",
            status_code=413
        )


    # -----------------------------------------------------
    # Create a temporary PDF file
    #
    # IMPORTANT:
    # delete=False is intentional here.
    #
    # On Windows, NamedTemporaryFile(delete=True) keeps
    # the file locked. pdfplumber then cannot open it.
    # -----------------------------------------------------

    temp_pdf = tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False
    )

    try:

        # -------------------------------------------------
        # Save uploaded PDF
        # -------------------------------------------------

        temp_pdf.write(content)
        temp_pdf.close()


        # -------------------------------------------------
        # Extract text from CIBIL PDF
        # -------------------------------------------------

        text = extract_cibil.load_text(temp_pdf.name)


        # -------------------------------------------------
        # Detect CIBIL report template
        # -------------------------------------------------

        template_type = extract_cibil.detect_template(text)


        # -------------------------------------------------
        # Extract structured data
        # -------------------------------------------------

        if template_type == "B":

            data = extract_cibil.extract_template_b(text)


        elif template_type == "A":

            enq = extract_cibil.extract_enquiries(text)

            data = {
                "name": extract_cibil.extract_name(text),

                "full_name_as_reported":
                    extract_cibil.extract_full_name(text),

                "dob":
                    extract_cibil.extract_dob(text),

                "member_id":
                    extract_cibil.extract_member_id(text),

                "report_meta":
                    extract_cibil.extract_report_meta(text),

                "bureau_score":
                    extract_cibil.extract_bureau_score(text),

                "pan_numbers":
                    extract_cibil.extract_all_pans(text),

                "primary_pan":
                    extract_cibil.extract_primary_pan(text),

                "ckyc_number":
                    extract_cibil.extract_ckyc(text),

                "phone_numbers":
                    extract_cibil.extract_phones(text),

                "primary_phone":
                    extract_cibil.extract_primary_phone(text),

                "emails":
                    extract_cibil.extract_emails(text),

                "addresses":
                    extract_cibil.extract_addresses(
                        temp_pdf.name,
                        text
                    ),

                "primary_address":
                    extract_cibil.extract_primary_address(text),

                "account_summary":
                    extract_cibil.extract_summary(text),

                "enquiry_summary":
                    extract_cibil.extract_enquiry_summary(text),

                "accounts":
                    extract_cibil.extract_accounts(text),

                "enquiries":
                    enq["rows"],

                "enquiries_unparsed_rows":
                    enq["unparsed_rows"],
            }


        else:

            return HTMLResponse(
                """
                <h2>Unrecognized CIBIL PDF format</h2>

                <p>
                    This CIBIL report format is not currently supported.
                </p>
                """,
                status_code=400
            )


        # -------------------------------------------------
        # Add template information
        # -------------------------------------------------

        data["template"] = template_type


        # -------------------------------------------------
        # Run data quality checks
        # -------------------------------------------------

        data["data_checks"] = (
            extract_cibil.build_data_checks(data)
        )


        # -------------------------------------------------
        # Load a fresh copy of the dashboard template
        #
        # We do NOT modify the actual HTML file.
        # -------------------------------------------------

        soup = BeautifulSoup(
            BASE_TEMPLATE,
            "html.parser"
        )


        # -------------------------------------------------
        # Set "as of" date used by dashboard
        # -------------------------------------------------

        (
            populate_dashboard.CTX["as_of"],
            populate_dashboard.CTX["as_of_src"]
        ) = populate_dashboard.resolve_as_of(
            "report",
            data
        )


        # -------------------------------------------------
        # Populate main dashboard
        # -------------------------------------------------

        populate_dashboard.populate(
            data,
            soup
        )


        # -------------------------------------------------
        # Populate account/enquiry detail sections
        # -------------------------------------------------

        populate_dashboard.populate_details(
            data,
            soup
        )


        # -------------------------------------------------
        # Return populated dashboard to browser
        # -------------------------------------------------

        return HTMLResponse(
            content=str(soup)
        )

    except Exception:

        return HTMLResponse(
            content="""
            <h2>CIBIL report processing failed</h2>
            <p>
                We could not process this report.
                Please verify that you uploaded a valid CIBIL PDF
                and try again.
            </p>
            """,
            status_code=500
        )

    finally:

        # -------------------------------------------------
        # ALWAYS delete the temporary uploaded PDF
        #
        # This is important because CIBIL reports contain
        # highly sensitive personal and financial information.
        # -------------------------------------------------

        try:

            if os.path.exists(temp_pdf.name):
                os.unlink(temp_pdf.name)

        except Exception:

            # Do not replace the actual processing error
            # with a cleanup error.
            pass

# ---------------------------------------------------------
# Analyze uploaded CRIF PDF
# ---------------------------------------------------------

@app.post("/analyze/crif", response_class=HTMLResponse)
async def analyze_crif(file: UploadFile = File(...)):

    # -----------------------------------------------------
    # Basic file validation
    # -----------------------------------------------------

    if not file.filename:
        return HTMLResponse(
            "<h2>No file selected.</h2>",
            status_code=400
        )

    if not file.filename.lower().endswith(".pdf"):
        return HTMLResponse(
            "<h2>Please upload a PDF file.</h2>",
            status_code=400
        )
    
    content = await file.read()

    if len(content) > MAX_UPLOAD_SIZE:
        return HTMLResponse(
            "<h2>File is too large.</h2>"
            "<p>Please upload a PDF smaller than 10 MB.</p>",
            status_code=413
        )

    # -----------------------------------------------------
    # Create temporary PDF file
    # -----------------------------------------------------

    temp_pdf = tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False
    )

    try:

        # -------------------------------------------------
        # Save uploaded PDF
        # -------------------------------------------------

        temp_pdf.write(content)
        temp_pdf.close()

        # -------------------------------------------------
        # Extract structured CRIF data
        # -------------------------------------------------

        data = crif_extractor.extract(
            temp_pdf.name
        )

        # -------------------------------------------------
        # Populate a fresh copy of the CRIF dashboard
        #
        # CRIF dashboard_populator works with an HTML
        # string and returns the populated HTML string.
        # -------------------------------------------------

        populated_html = dashboard_populator.populate(
            CRIF_BASE_TEMPLATE,
            data,
            date.today()
        )

        # -------------------------------------------------
        # Return populated dashboard
        # -------------------------------------------------

        return HTMLResponse(
            content=populated_html
        )

    except Exception as e:

        return HTMLResponse(
            content=f"""
            <h2>CRIF report processing failed</h2>
            <p> We could not process this report.
            Please verify that you uploaded a valid CRIF PDF
            and try again. </p>
            """,
            status_code=500
        )

    finally:

        # -------------------------------------------------
        # ALWAYS delete temporary uploaded PDF
        # -------------------------------------------------

        try:

            if os.path.exists(temp_pdf.name):
                os.unlink(temp_pdf.name)

        except Exception:

            pass