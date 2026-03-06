"""Output parsers for playwright-cli command results."""

import json
import re
from typing import Any, Dict, List, Optional

from . import PlaywrightServiceError


def _try_json_list(output: str) -> Optional[list]:
    """Return empty list if output is blank, parsed list if valid JSON list, else None."""
    stripped = output.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return None


def _iter_content_lines(text: str):
    """Yield non-empty, non-comment lines with bullet/dash prefix stripped."""
    for line in text.split('\n'):
        line = line.strip().lstrip('- ')
        if line and not line.startswith('#'):
            yield line


def _parse_markdown_sections(output: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_section = None
    current_lines: List[str] = []
    for line in output.split('\n'):
        if line.startswith('### '):
            if current_section is not None:
                sections[current_section] = '\n'.join(current_lines).strip()
            current_section = line[4:].strip()
            current_lines = []
        elif current_section is not None:
            current_lines.append(line)
    if current_section is not None:
        sections[current_section] = '\n'.join(current_lines).strip()
    return sections


def _parse_page_section(content: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        'url': '', 'title': '', 'console_errors': 0, 'console_warnings': 0,
    }
    for line in content.split('\n'):
        line = line.strip().lstrip('- ')
        if line.startswith('Page URL:'):
            info['url'] = line[9:].strip()
        elif line.startswith('Page Title:'):
            info['title'] = line[11:].strip()
        elif line.startswith('Console'):
            m = re.search(r'(\d+)\s+error', line)
            if m:
                info['console_errors'] = int(m.group(1))
            m = re.search(r'(\d+)\s+warning', line)
            if m:
                info['console_warnings'] = int(m.group(1))
    return info


def _parse_file_path_section(content: str) -> Optional[str]:
    """Parse a markdown section to extract a file path (snapshot, screenshot, pdf, etc.)."""
    for line in _iter_content_lines(content):
        m = re.match(r'\[.*?\]\((.+?)\)', line)
        if m:
            return m.group(1)
        path = line.strip('`').strip()
        if path:
            return path
    return None


def _parse_action_output(output: str) -> Dict[str, Any]:
    sections = _parse_markdown_sections(output)
    result: Dict[str, Any] = {}
    if 'Page' in sections:
        result['page'] = _parse_page_section(sections['Page'])
    if 'Snapshot' in sections:
        result['snapshot_file'] = _parse_file_path_section(sections['Snapshot'])
    if 'Result' in sections:
        result['result'] = _parse_file_path_section(sections['Result'])
    return result


def _parse_session_list(output: str) -> List[Dict[str, Any]]:
    output = output.strip()
    if not output:
        return []
    sessions = []
    current_session: Optional[Dict[str, Any]] = None
    for line in output.split('\n'):
        if line.startswith('#'):
            continue
        m = re.match(r'^- (\S+):$', line)
        if m:
            if current_session:
                sessions.append(current_session)
            current_session = {'name': m.group(1)}
            continue
        if current_session is not None:
            m = re.match(r'^\s+- ([\w-]+):\s*(.*)$', line)
            if m:
                key = m.group(1).replace('-', '_')
                value = m.group(2).strip()
                if key == 'browser_type':
                    current_session['browser_type'] = value
                elif key == 'user_data_dir':
                    current_session['user_data_dir'] = value
                elif key == 'headed':
                    current_session['headed'] = value.lower() == 'true'
                elif key == 'pid':
                    current_session['pid'] = int(value) if value.isdigit() else None
                elif key == 'status':
                    current_session['status'] = value
    if current_session:
        sessions.append(current_session)
    return sessions


def _parse_tab_list(output: str) -> List[Dict[str, Any]]:
    tabs = []
    for line in output.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^(\d+):\s+(.+?)(?:\s+-\s+(.+))?$', line)
        if m:
            tab = {'index': int(m.group(1)), 'url': m.group(2).strip()}
            if m.group(3):
                tab['title'] = m.group(3).strip()
            tabs.append(tab)
        else:
            tabs.append({'index': len(tabs), 'url': line})
    return tabs


def _parse_cookie_list(output: str) -> List[Dict[str, Any]]:
    result = _try_json_list(output)
    if result is not None:
        return result
    output = output.strip()
    cookies = []
    sections = output.split('\n\n')
    for section in sections:
        section = section.strip()
        if not section:
            continue
        cookie: Dict[str, Any] = {}
        for line in section.split('\n'):
            line = line.strip().lstrip('- ')
            if ':' in line:
                key, _, val = line.partition(':')
                key = key.strip().lower().replace(' ', '')
                val = val.strip()
                if key == 'name':
                    cookie['name'] = val
                elif key == 'value':
                    cookie['value'] = val
                elif key == 'domain':
                    cookie['domain'] = val
                elif key == 'path':
                    cookie['path'] = val
                elif key == 'expires':
                    cookie['expires'] = val
                elif key == 'httponly':
                    cookie['httpOnly'] = val.lower() in ('true', 'yes', '1')
                elif key == 'secure':
                    cookie['secure'] = val.lower() in ('true', 'yes', '1')
                elif key == 'samesite':
                    cookie['sameSite'] = val
            elif not cookie and line and '=' in line:
                name, _, value = line.partition('=')
                cookie['name'] = name.strip()
                cookie['value'] = value.strip()
        if cookie and cookie.get('name'):
            cookies.append(cookie)
    if not cookies:
        for line in output.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                name, _, value = line.partition('=')
                cookies.append({'name': name.strip(), 'value': value.strip()})
            elif ':' in line:
                name, _, value = line.partition(':')
                cookies.append({'name': name.strip(), 'value': value.strip()})
    return cookies


def _parse_storage_list(output: str) -> List[Dict[str, str]]:
    output = output.strip()
    if not output:
        return []
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            return [{'key': k, 'value': str(v)} for k, v in data.items()]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    items = []
    for line in _iter_content_lines(output):
        if ':' in line:
            key, _, value = line.partition(':')
            items.append({'key': key.strip(), 'value': value.strip()})
        elif '=' in line:
            key, _, value = line.partition('=')
            items.append({'key': key.strip(), 'value': value.strip()})
    return items


def _parse_storage_get(output: str) -> Optional[str]:
    output = output.strip()
    if not output:
        return None
    if ':' in output:
        _, _, value = output.partition(':')
        return value.strip()
    return output


def _parse_network_requests(output: str) -> List[Dict[str, Any]]:
    result = _try_json_list(output)
    if result is not None:
        return result
    requests = []
    for line in _iter_content_lines(output):
        parts = line.split()
        if len(parts) >= 2:
            req: Dict[str, Any] = {'method': parts[0]}
            if len(parts) >= 3 and parts[1].isdigit():
                req['status'] = int(parts[1])
                req['url'] = parts[2]
                if len(parts) >= 4:
                    req['content_type'] = parts[3]
            else:
                req['url'] = parts[1]
                if len(parts) >= 3:
                    if parts[2].isdigit():
                        req['status'] = int(parts[2])
                    else:
                        req['content_type'] = parts[2]
            requests.append(req)
    return requests


def _parse_route_list(output: str) -> List[Dict[str, str]]:
    if not output.strip():
        return []
    return [{'pattern': line} for line in _iter_content_lines(output)]


def _parse_console_messages(output: str) -> List[Dict[str, Any]]:
    result = _try_json_list(output)
    if result is not None:
        return result
    messages = []
    for line in output.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        msg: Dict[str, Any] = {'level': 'INFO', 'text': line}
        m = re.match(r'^\[(\w+)\]\s+(.+)$', line)
        if m:
            msg['level'] = m.group(1).upper()
            msg['text'] = m.group(2)
        url_match = re.search(r'(https?://\S+):(\d+)', msg['text'])
        if url_match:
            msg['url'] = url_match.group(1)
            msg['line'] = int(url_match.group(2))
        messages.append(msg)
    return messages


def _parse_eval_result(stdout: str) -> Any:
    """Extract the result field from playwright page eval JSON output.

    Handles two output formats:
    1. Raw JSON: ``{"result": ...}``
    2. Markdown: ``### Result\\n<json>\\n### Ran Playwright code ...``

    Raises PlaywrightServiceError if the output contains ``### Error``.
    """
    text = stdout.strip()
    if not text:
        return None

    # Detect error output from playwright CLI (### Error\nError: ...)
    if text.startswith("### Error"):
        lines = text.split("\n")
        error_lines = []
        capturing = False
        for line in lines:
            if line.startswith("### Error"):
                capturing = True
                continue
            if line.startswith("### ") and capturing:
                break
            if capturing:
                error_lines.append(line)
        error_text = "\n".join(error_lines).strip() or "Unknown playwright error"
        raise PlaywrightServiceError(error_text)

    # Handle markdown-formatted output (### Result ... ### Ran Playwright code)
    if text.startswith("### Result"):
        lines = text.split("\n")
        json_lines = []
        capturing = False
        for line in lines:
            if line.startswith("### Result"):
                capturing = True
                continue
            if line.startswith("### ") and capturing:
                break
            if capturing:
                json_lines.append(line)
        json_text = "\n".join(json_lines).strip()
        if json_text:
            if json_text in ("undefined", "null"):
                return None
            try:
                return json.loads(json_text)
            except (json.JSONDecodeError, ValueError):
                return json_text
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text if text not in ("undefined", "null") else None
    if isinstance(data, dict) and "result" in data:
        raw = data["result"]
    else:
        raw = data
    if raw is None or raw == "null" or raw == "undefined":
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw
