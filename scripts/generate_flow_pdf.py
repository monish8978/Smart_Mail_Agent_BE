import os
import sys
import subprocess

# Auto-install reportlab if it's not present
try:
    import reportlab
except ImportError:
    print("ReportLab not found. Trying to install reportlab...")
    try:
        # Try standard pip install first
        subprocess.check_call([sys.executable, "-m", "pip", "install", "reportlab"])
    except subprocess.CalledProcessError:
        print("Standard pip install blocked or failed. Retrying with --break-system-packages...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", "reportlab"])
        except subprocess.CalledProcessError:
            print("Pip install failed. Please run 'apt install python3-reportlab' or run within a virtual environment.")
            raise
    import reportlab

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon, Group, Circle

def create_flow_diagram():
    # Width 500, Height 320
    d = Drawing(500, 320)
    
    # Background Canvas Box
    d.add(Rect(0, 0, 500, 320, fillColor=colors.HexColor('#f8fafc'), strokeColor=colors.HexColor('#e2e8f0'), strokeWidth=1, rx=8, ry=8))
    
    # 1. Ingestion Sources
    # Rounded Rect for Sources
    d.add(Rect(15, 250, 95, 45, fillColor=colors.HexColor('#1e293b'), strokeColor=colors.HexColor('#0f172a'), strokeWidth=1, rx=5, ry=5))
    d.add(String(62, 278, "EMAIL INGEST", textAnchor="middle", fillColor=colors.white, fontSize=9, fontName="Helvetica-Bold"))
    d.add(String(62, 263, "(IMAP / REST API)", textAnchor="middle", fillColor=colors.HexColor('#cbd5e1'), fontSize=8, fontName="Helvetica"))
    
    # Arrow from Ingestion to Celery Worker
    d.add(Line(110, 272, 140, 272, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    d.add(Line(135, 275, 140, 272, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    d.add(Line(135, 269, 140, 272, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    
    # 2. Celery Task Queue
    d.add(Rect(140, 250, 100, 45, fillColor=colors.HexColor('#0d9488'), strokeColor=colors.HexColor('#0f766e'), strokeWidth=1, rx=5, ry=5))
    d.add(String(190, 278, "CELERY WORKER", textAnchor="middle", fillColor=colors.white, fontSize=9, fontName="Helvetica-Bold"))
    d.add(String(190, 263, "process_email_task", textAnchor="middle", fillColor=colors.HexColor('#ccfbf1'), fontSize=8, fontName="Helvetica-Oblique"))
    
    # Arrow from Celery to Email Bot
    d.add(Line(190, 250, 190, 205, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    d.add(Line(187, 210, 190, 205, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    d.add(Line(193, 210, 190, 205, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    
    # 3. Email Bot (The Black Box)
    d.add(Rect(110, 130, 160, 75, fillColor=colors.HexColor('#1e40af'), strokeColor=colors.HexColor('#1e3a8a'), strokeWidth=1.5, rx=6, ry=6))
    d.add(String(190, 185, "EMAIL BOT (AI BOX)", textAnchor="middle", fillColor=colors.white, fontSize=11, fontName="Helvetica-Bold"))
    d.add(String(190, 168, "• Intent & Order ID Search", textAnchor="middle", fillColor=colors.HexColor('#dbeafe'), fontSize=8, fontName="Helvetica"))
    d.add(String(190, 153, "• RAG / CRM Order API Status", textAnchor="middle", fillColor=colors.HexColor('#dbeafe'), fontSize=8, fontName="Helvetica"))
    d.add(String(190, 138, "• Draft Reply & LLM Scoring", textAnchor="middle", fillColor=colors.HexColor('#dbeafe'), fontSize=8, fontName="Helvetica"))
    
    # Bidirectional Line to CRM API (Fetching Status)
    d.add(Line(270, 167, 340, 167, strokeColor=colors.HexColor('#2563eb'), strokeWidth=1.2, strokeDashArray=[2, 2]))
    # Arrow heads for bidirectional communication
    d.add(Line(275, 170, 270, 167, strokeColor=colors.HexColor('#2563eb'), strokeWidth=1.2))
    d.add(Line(275, 164, 270, 167, strokeColor=colors.HexColor('#2563eb'), strokeWidth=1.2))
    d.add(Line(335, 170, 340, 167, strokeColor=colors.HexColor('#2563eb'), strokeWidth=1.2))
    d.add(Line(335, 164, 340, 167, strokeColor=colors.HexColor('#2563eb'), strokeWidth=1.2))
    
    d.add(String(305, 172, "Fetch Status", textAnchor="middle", fillColor=colors.HexColor('#1e3a8a'), fontSize=7, fontName="Helvetica-Bold"))
    
    # 4. CRM Status API Box
    d.add(Rect(340, 145, 130, 45, fillColor=colors.HexColor('#eff6ff'), strokeColor=colors.HexColor('#3b82f6'), strokeWidth=1, rx=4, ry=4))
    d.add(String(405, 173, "CRM FETCH API", textAnchor="middle", fillColor=colors.HexColor('#1e40af'), fontSize=8, fontName="Helvetica-Bold"))
    d.add(String(405, 161, "Endpoint: /order-status", textAnchor="middle", fillColor=colors.HexColor('#475569'), fontSize=7, fontName="Helvetica-Bold"))
    d.add(String(405, 151, "Returns Docket/Ticket Info", textAnchor="middle", fillColor=colors.HexColor('#64748b'), fontSize=7, fontName="Helvetica"))

    # Arrow from Email Bot to Decision Gate
    d.add(Line(190, 130, 190, 95, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    d.add(Line(187, 100, 190, 95, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    d.add(Line(193, 100, 190, 95, strokeColor=colors.HexColor('#64748b'), strokeWidth=1.5))
    d.add(String(225, 110, "Score + Decision", textAnchor="start", fillColor=colors.HexColor('#475569'), fontSize=8, fontName="Helvetica"))

    # 5. Decision Gate (Confidence vs Threshold & Floor Check)
    # Drawing a diamond polygon
    # Center is at (190, 60), half-width=55, half-height=35
    d.add(Polygon([135, 60, 190, 95, 245, 60, 190, 25], fillColor=colors.HexColor('#ea580c'), strokeColor=colors.HexColor('#c2410c'), strokeWidth=1.2))
    d.add(String(190, 68, "DECISION GATE", textAnchor="middle", fillColor=colors.white, fontSize=8, fontName="Helvetica-Bold"))
    d.add(String(190, 56, "Score > Thr &", textAnchor="middle", fillColor=colors.white, fontSize=7, fontName="Helvetica"))
    d.add(String(190, 46, "Context Loaded?", textAnchor="middle", fillColor=colors.white, fontSize=7, fontName="Helvetica"))
    
    # Branch A: YES (Auto Send)
    # Line going left
    d.add(Line(135, 60, 85, 60, strokeColor=colors.HexColor('#16a34a'), strokeWidth=1.5))
    d.add(Line(85, 60, 85, 80, strokeColor=colors.HexColor('#16a34a'), strokeWidth=1.5))
    d.add(Line(82, 75, 85, 80, strokeColor=colors.HexColor('#16a34a'), strokeWidth=1.5))
    d.add(Line(88, 75, 85, 80, strokeColor=colors.HexColor('#16a34a'), strokeWidth=1.5))
    d.add(String(110, 65, "YES", textAnchor="middle", fillColor=colors.HexColor('#15803d'), fontSize=8, fontName="Helvetica-Bold"))
    
    # Auto-Send Box
    d.add(Rect(30, 80, 110, 45, fillColor=colors.HexColor('#f0fdf4'), strokeColor=colors.HexColor('#16a34a'), strokeWidth=1, rx=5, ry=5))
    d.add(String(85, 107, "AUTO-SEND REPLY", textAnchor="middle", fillColor=colors.HexColor('#166534'), fontSize=9, fontName="Helvetica-Bold"))
    d.add(String(85, 95, "Sends draft directly", textAnchor="middle", fillColor=colors.HexColor('#166534'), fontSize=7, fontName="Helvetica"))
    d.add(String(85, 85, "via SMTP Mailer", textAnchor="middle", fillColor=colors.HexColor('#475569'), fontSize=7, fontName="Helvetica"))
    
    # Branch B: NO (Escalate / Create Ticket in CRM)
    # Line going right
    d.add(Line(245, 60, 340, 60, strokeColor=colors.HexColor('#dc2626'), strokeWidth=1.5))
    d.add(Line(335, 63, 340, 60, strokeColor=colors.HexColor('#dc2626'), strokeWidth=1.5))
    d.add(Line(335, 57, 340, 60, strokeColor=colors.HexColor('#dc2626'), strokeWidth=1.5))
    d.add(String(280, 65, "NO (Low Score / No Context)", textAnchor="middle", fillColor=colors.HexColor('#b91c1c'), fontSize=8, fontName="Helvetica-Bold"))
    
    # Create Ticket CRM API Box
    d.add(Rect(340, 30, 130, 60, fillColor=colors.HexColor('#fef2f2'), strokeColor=colors.HexColor('#dc2626'), strokeWidth=1, rx=5, ry=5))
    d.add(String(405, 75, "CRM CREATE TICKET", textAnchor="middle", fillColor=colors.HexColor('#991b1b'), fontSize=9, fontName="Helvetica-Bold"))
    d.add(String(405, 63, "Endpoint: /create-ticket", textAnchor="middle", fillColor=colors.HexColor('#475569'), fontSize=7.5, fontName="Helvetica-Bold"))
    d.add(String(405, 51, "1. API creates CRM Ticket", textAnchor="middle", fillColor=colors.HexColor('#dc2626'), fontSize=7, fontName="Helvetica"))
    d.add(String(405, 41, "2. Sends Ticket ID to Client", textAnchor="middle", fillColor=colors.HexColor('#dc2626'), fontSize=7, fontName="Helvetica"))
    
    return d

def generate_pdf(output_path):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=0.5*inch,
        rightMargin=0.5*inch,
        topMargin=0.5*inch,
        bottomMargin=0.5*inch
    )
    
    styles = getSampleStyleSheet()
    
    # Custom Palette
    c_primary = colors.HexColor('#1e3a8a')  # Dark Blue
    c_secondary = colors.HexColor('#0d9488')  # Teal
    c_text = colors.HexColor('#334155')  # Charcoal
    c_bg_light = colors.HexColor('#f8fafc')
    c_accent_red = colors.HexColor('#b91c1c')  # Dark Red
    
    # Modify Styles
    styles['Normal'].textColor = c_text
    styles['Normal'].fontSize = 9.5
    styles['Normal'].leading = 13.5
    
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=22,
        leading=26,
        textColor=c_primary,
        alignment=0, # Left
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        textColor=c_secondary,
        alignment=0,
    )
    
    h1_style = ParagraphStyle(
        'SectionH1',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=c_primary,
        spaceBefore=14,
        spaceAfter=6,
        keepWithNext=True
    )
    
    h2_style = ParagraphStyle(
        'SectionH2',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=15,
        textColor=c_secondary,
        spaceBefore=8,
        spaceAfter=4,
        keepWithNext=True
    )
    
    code_style = ParagraphStyle(
        'CodeStyle',
        parent=styles['Normal'],
        fontName='Courier',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#0f172a'),
    )
    
    table_text = ParagraphStyle(
        'TableText',
        parent=styles['Normal'],
        fontSize=8,
        leading=10,
    )
    
    table_header = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.white,
    )
    
    story = []
    
    # ------------------ HEADER SECTOR ------------------
    story.append(Paragraph("AI EMAIL BOT ARCHITECTURE", title_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph("System Architecture & Decision Engine Flow Specification", subtitle_style))
    story.append(Spacer(1, 8))
    
    # Horizontal Rule
    story.append(Table(
        [['']],
        colWidths=[7.5*inch],
        rowHeights=[2],
        style=TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), c_primary),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
        ])
    ))
    story.append(Spacer(1, 10))
    
    # ------------------ SUMMARY ------------------
    summary_text = (
        "<b>Executive Summary:</b> The AI Email Bot leverages Large Language Models (LLMs) and "
        "Retrieval-Augmented Generation (RAG) to automate email support. By intercepting incoming emails "
        "and evaluating response confidence against a client-specific dynamic threshold, the system acts as "
        "an intelligent first responder. It autonomously replies to high-confidence requests while seamlessly "
        "escalating low-confidence and missing-context queries to the CRM system by creating formal support tickets."
    )
    story.append(Paragraph(summary_text, styles['Normal']))
    story.append(Spacer(1, 10))
    
    # ------------------ STATE MACHINE DIAGRAM ------------------
    story.append(Paragraph("1. System Flow & Decision State Machine Diagram", h1_style))
    story.append(Paragraph(
        "The following diagram illustrates how the system ingests data, queries RAG or CRM Status APIs based on intent, "
        "evaluates draft responses via LLM scoring, and either triggers direct mail delivery or scales up to a CRM Ticket "
        "via a customized JSON-over-Base64 HTTP webhook call.",
        styles['Normal']
    ))
    story.append(Spacer(1, 10))
    
    # Add Vector Diagram
    story.append(create_flow_diagram())
    story.append(Spacer(1, 15))
    
    # ------------------ STEP BY STEP EXPLANATION ------------------
    story.append(Paragraph("2. Step-by-Step Processing Pipeline", h1_style))
    
    steps = [
        ("Step 1: Data Ingestion", 
         "Incoming emails are fetched continuously from the registered email inbox via an IMAP listener, or pushed via a FastAPI REST endpoint (<code>POST /process-email</code>). These details are packed and queued asynchronously into Redis, then consumed by a background Celery task (<code>process_email_task</code>)."),
        
        ("Step 2: Email Bot AI Box (Abstracted)", 
         "Inside the email bot processing unit: <b>1) Intent Detection:</b> The LLM parses the query to extract the intent and check for ticket or docket numbers. <b>2) Context Loading:</b> If asking for ticket status, it calls the <i>CRM Fetch Ticket Status API</i>; otherwise, it falls back to querying the ChromaDB RAG vector space. <b>3) Draft Generation & Scoring:</b> The LLM generates a drafted response based on the fetched context and scores the draft (0-100) based on relevance."),
        
        ("Step 3: Confidence Score & Threshold Gate", 
         "The Celery worker dynamically fetches the client-specific confidence threshold (e.g. 80%). If RAG / API returned no context (<b>Hard Floor Trigger</b>) or the generated response score is below or equal to the threshold, the bot escalates. If the score is higher than the threshold and context succeeded, it routes to auto-send."),
        
        ("Step 4: Response Actions", 
         "<b>Path A (Auto-Send):</b> The system sends the high-confidence reply directly to the customer via SMTP and marks status as <code>sent</code>.<br/>"
         "<b>Path B (CRM Ticket Escalation):</b> The system executes the <i>CRM Create Ticket Webhook</i>. The webhook generates a unique CRM Ticket ID (e.g., <code>T-260526-00123</code>). The local database records this ticket. A separate, formatted ticket notification is composed (e.g., 'Ticket T-123 Created') and sent to the client via email.")
    ]
    
    for title, desc in steps:
        story.append(Paragraph(f"<b>• {title}:</b> {desc}", styles['Normal']))
        story.append(Spacer(1, 4))
    
    story.append(Spacer(1, 10))
    
    # Page Break for API specifications
    story.append(PageBreak())
    
    # ------------------ WEBHOOK / API CALLED ------------------
    story.append(Paragraph("3. Webhook & API Integrations", h1_style))
    story.append(Paragraph(
        "To fetch order/ticket status and escalate low-confidence emails, the platform calls custom, "
        "client-configured HTTP GET webhooks. These webhooks utilize JSON payloads packed inside Base64 url-encoded variables "
        "to interact securely and dynamic with the underlying CRM system.",
        styles['Normal']
    ))
    story.append(Spacer(1, 10))
    
    # Table of Webhooks/APIs
    story.append(Paragraph("Active API Endpoints Call Specification", h2_style))
    
    api_table_data = [
        [
            Paragraph("API / Webhook Function", table_header),
            Paragraph("HTTP Method", table_header),
            Paragraph("Dynamic Base URL (Database Loaded)", table_header),
            Paragraph("Query Parameter", table_header)
        ],
        [
            Paragraph("<b>Create CRM Ticket</b><br/>Escalates low score/missing context emails to create a ticket in CRM.", table_text),
            Paragraph("<code>GET</code>", table_text),
            Paragraph("<code>db_create_payload_table[\"url\"]</code><br/>e.g., <i>https://crm.c-zentrix.com/api/ticket</i>", table_text),
            Paragraph("<code>?data=&lt;Base64_JSON&gt;</code>", table_text)
        ],
        [
            Paragraph("<b>Fetch Ticket Status</b><br/>Checks status of existing ticket using order/docket ID.", table_text),
            Paragraph("<code>GET</code>", table_text),
            Paragraph("<code>get_payload_get_ticket_table[\"url\"]</code><br/>e.g., <i>https://crm.c-zentrix.com/api/status</i>", table_text),
            Paragraph("<code>?postData=&lt;Base64_JSON&gt;</code>", table_text)
        ]
    ]
    
    t_api = Table(api_table_data, colWidths=[2.2*inch, 0.9*inch, 2.7*inch, 1.7*inch])
    t_api.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), c_primary),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, c_bg_light])
    ]))
    story.append(t_api)
    story.append(Spacer(1, 15))
    
    # ------------------ DETAILED PAYLOADS ------------------
    story.append(Paragraph("4. Technical Payload Schemas", h1_style))
    story.append(Paragraph(
        "The following JSON structures illustrate the schema before being Base64-encoded and appended "
        "to the respective webhook endpoints.",
        styles['Normal']
    ))
    story.append(Spacer(1, 8))
    
    # Create Ticket Schema and Fetch Ticket Schema in side-by-side or stacked Tables
    create_payload_json = (
        "{\n"
        "  \"mail_id\": \"customer@example.com\",  // Sender's email\n"
        "  \"subject\": \"Reset password request\",\n"
        "  \"body\": \"I cannot log into my account. Please help.\",\n"
        "  \"status\": \"Ticket_Generated\",\n"
        "  \"client_specific_key\": \"client_val\" // Pre-configured\n"
        "}"
    )
    
    fetch_payload_json = (
        "{\n"
        "  \"filter\": {\n"
        "    \"docket_no\": \"T-260526-00431\" // Ticket / Docket Number\n"
        "  },\n"
        "  \"client_specific_key\": \"client_val\" // Pre-configured\n"
        "}"
    )
    
    schemas_table_data = [
        [
            Paragraph("<b>Create CRM Ticket Webhook Payload (Decoded JSON)</b>", ParagraphStyle('H', parent=styles['Normal'], fontName='Helvetica-Bold', textColor=c_primary)),
            Paragraph("<b>Fetch Ticket Status Webhook Payload (Decoded JSON)</b>", ParagraphStyle('H', parent=styles['Normal'], fontName='Helvetica-Bold', textColor=c_primary))
        ],
        [
            Paragraph(f"<font face='Courier' size='7'>{create_payload_json.replace(chr(10), '<br/>').replace(' ', '&nbsp;')}</font>", table_text),
            Paragraph(f"<font face='Courier' size='7'>{fetch_payload_json.replace(chr(10), '<br/>').replace(' ', '&nbsp;')}</font>", table_text)
        ]
    ]
    
    t_schemas = Table(schemas_table_data, colWidths=[3.75*inch, 3.75*inch])
    t_schemas.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#eff6ff')),
        ('BACKGROUND', (0,1), (0,1), colors.HexColor('#f8fafc')),
        ('BACKGROUND', (1,1), (1,1), colors.HexColor('#f8fafc')),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(t_schemas)
    story.append(Spacer(1, 15))
    
    # ------------------ CRM RESPONSE DETAILS ------------------
    story.append(Paragraph("Expected Response Objects", h2_style))
    
    resp_create_json = (
        "{\n"
        "  \"Status\": \"Success\",\n"
        "  \"Message\": \"Ticket created. Refrence_No is T-260526-00431\",\n"
        "  \"Refrence_No\": \"T-260526-00431\"\n"
        "}"
    )
    
    resp_fetch_json = (
        "{\n"
        "  \"Success\": {\n"
        "    \"ticket_1\": {\n"
        "      \"docket_no\": \"T-260526-00431\",\n"
        "      \"ticket_status\": \"Open\",\n"
        "      \"priority_name\": \"Normal\",\n"
        "      \"ticket_type\": \"Support\",\n"
        "      \"problem_reported\": \"I cannot log into my account.\",\n"
        "      \"agent_remarks\": \"Assigned to tech support\",\n"
        "      \"person\": {\n"
        "        \"person_name\": \"John Doe\",\n"
        "        \"person_mail\": \"customer@example.com\"\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}"
    )
    
    resp_table_data = [
        [
            Paragraph("<b>Create CRM Ticket Webhook Response</b>", ParagraphStyle('H', parent=styles['Normal'], fontName='Helvetica-Bold', textColor=c_secondary)),
            Paragraph("<b>Fetch Ticket Status Webhook Response</b>", ParagraphStyle('H', parent=styles['Normal'], fontName='Helvetica-Bold', textColor=c_secondary))
        ],
        [
            Paragraph(f"<font face='Courier' size='7'>{resp_create_json.replace(chr(10), '<br/>').replace(' ', '&nbsp;')}</font>", table_text),
            Paragraph(f"<font face='Courier' size='7'>{resp_fetch_json.replace(chr(10), '<br/>').replace(' ', '&nbsp;')}</font>", table_text)
        ]
    ]
    
    t_resp = Table(resp_table_data, colWidths=[3.75*inch, 3.75*inch])
    t_resp.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f0fdfa')),
        ('BACKGROUND', (0,1), (0,1), colors.HexColor('#f8fafc')),
        ('BACKGROUND', (1,1), (1,1), colors.HexColor('#f8fafc')),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(t_resp)
    
    # Build Document
    doc.build(story)
    print(f"Successfully generated PDF at {output_path}!")

if __name__ == "__main__":
    output_pdf = "Email_Bot_Architecture.pdf"
    if len(sys.argv) > 1:
        output_pdf = sys.argv[1]
    
    generate_pdf(output_pdf)
