"""Regression coverage for Site Infrastructure and the PDF-only severity policy."""
import io

import pytest

from app.services import gap_assessment_report as gap
from app.services import gap_report_template as template


def _site(count=1):
    return {'client_name': 'Example', 'num_endpoints': '74', 'switch': 'Core 48p',
            'router': 'Edge router', 'email_summary': 'Hosted email',
            'firewall': {'make_model': 'Fortinet 60F'},
            'server_inventory': [{'name': f'server-{i}', 'ip': f'10.0.0.{i+1}',
                                  'role': 'DB', 'operating_system': 'Ubuntu', 'location': 'Main DC'}
                                 for i in range(count)]}


def _ctx(count=1):
    return {'site': _site(count), 'client': 'Example', 'intro': 'Introduction',
            'report_date': '19-11-2026', 'findings': [], 'finding_summary': {},
            'assessment': {'verified': False}}


def test_server_inventory_strict_ip_and_optional_fields():
    pytest.importorskip('psycopg2')
    from app.routers.cases import GapSiteBody, SiteServerRecord
    record = SiteServerRecord(name='  APP-01  ', ip='2001:0db8::1', role='Application')
    assert record.name == 'APP-01' and record.ip == '2001:db8::1'
    with pytest.raises(ValueError):
        SiteServerRecord(name='invalid', ip='localhost')
    body = GapSiteBody(switch='48-port', router='branch', email_summary='Microsoft 365',
                       server_inventory=[record])
    assert body.model_dump()['server_inventory'][0]['name'] == 'APP-01'
    with pytest.raises(ValueError):
        GapSiteBody(server_inventory=[{'name': 'bad', 'ip': '999.888.1.0'}])


def test_server_inventory_html_and_page_index_for_large_site():
    ctx = _ctx(33)
    text = template.build_styled_gap_report_html(ctx, include_cover=False)
    assert 'server-32' in text and '10.0.0.33' in text
    assert 'Core 48p' in text and 'Edge router' in text and 'Hosted email' in text
    assert text.count('SERVER INVENTORY (NAME / IP):') == 3
    # 33 entries / 14 rows = 3 continuation pages, so summary starts at 9.
    assert template._compute_toc_page_map(findings_count=0, infrastructure_extra_pages=3)['summary'] == 9
    assert text.count('3. SITE INFRASTRUCTURE (CONTINUED)') >= 3
    fb = template._fallback_section_bodies(ctx)['infra']
    assert 'server-32' in fb and 'Hosted email' in fb


def test_merge_site_inventory_unions_unique_entries_and_preserves_other_sites():
    a = _site(1)
    b = _site(1)
    b['client_name'] = 'Another site'
    b['server_inventory'][0]['name'] = 'backup-server'
    merged = gap.merge_gap_sites([a, b, a])
    assert [r['name'] for r in merged['server_inventory']] == ['server-0', 'backup-server']
    assert merged['_merged_site_diverged'] is True
    html = template.build_styled_gap_report_html({**_ctx(), 'site': merged}, include_cover=False)
    assert 'Another site' in html
    assert 'backup-server' in html


def test_pdf_low_severity_filter_does_not_change_context():
    original = {'site': _site(), 'client': 'Example', 'findings': [
        {'name': 'TLS', 'risk': 'High'}, {'name': 'Banner', 'risk': 'Low'}],
        'finding_summary': {'Critical': 0, 'High': 1, 'Medium': 0, 'Low': 1,
                            'Info': 0, 'total': 2, 'actionable': 2}}
    view = gap._pdf_view_without_low(original)
    assert [r['name'] for r in view['findings']] == ['TLS']
    assert view['finding_summary']['actionable'] == 1
    assert view['finding_summary']['total'] == 1
    assert 'Low' not in view['finding_summary']
    assert 'Low' in original['finding_summary']
    assert len(original['findings']) == 2
    html = template.build_styled_gap_report_html({**_ctx(), **view}, include_cover=False)
    assert 'TLS' in html and 'Banner' not in html
    assert '<th>Low</th>' not in html
    fallback = template._fallback_section_bodies({**_ctx(), **view})
    assert 'Banner' not in fallback['summary']
    assert '<th>Low</th>' not in fallback['summary']


def test_docx_includes_server_ip_and_infrastructure():
    document = pytest.importorskip('docx')
    data = gap.render_gap_docx(_ctx())
    doc = document.Document(io.BytesIO(data))
    body = '\n'.join(par.text for par in doc.paragraphs) + '\n' + '\n'.join(
        cell.text for table in doc.tables for row in table.rows for cell in row.cells)
    assert 'server-0' in body and '10.0.0.1' in body
    assert 'Core 48p' in body
