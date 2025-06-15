#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
테니스 코트 예약 현황 모니터링 스크립트
MonitoringTable.txt에 정의된 시설과 시간대를 기반으로
오늘부터 3일 후까지 예약 가능한 코트를 확인합니다.
"""

import requests
from bs4 import BeautifulSoup
import os
from datetime import datetime, timedelta
import time
import urllib3
import re
import logging
import shutil
from flask import Flask, render_template, jsonify
import threading

# SSL 경고 메시지 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
scheduler = None
monitoring_results = []

class TennisCourtScheduler:
    def __init__(self, username, password, monitoring_file="MonitoringTable.txt"):
        self.username = username
        self.password = password
        self.base_url = "https://res.isdc.co.kr"
        self.session = requests.Session()
        self.monitoring_file = monitoring_file
        self.facilities = []
        self.available_slots = []
        
        # 로그 디렉토리 생성
        self.log_dir = "log"
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
            
        # 일주일이 지난 파일 정리
        self.cleanup_old_files()
        
        # 로그 파일 설정
        self.setup_logging()
        
        # 모니터링 설정 로드
        self.load_monitoring_settings()

    def cleanup_old_files(self):
        """일주일이 지난 파일들을 삭제"""
        try:
            current_time = time.time()
            one_week_ago = current_time - (7 * 24 * 60 * 60)  # 7일을 초로 변환
            
            for filename in os.listdir(self.log_dir):
                filepath = os.path.join(self.log_dir, filename)
                if os.path.isfile(filepath):
                    file_time = os.path.getmtime(filepath)
                    if file_time < one_week_ago:
                        os.remove(filepath)
                        print(f"🗑️ 오래된 파일 삭제: {filename}")
        except Exception as e:
            print(f"❌ 파일 정리 중 오류 발생: {e}")

    def setup_logging(self):
        """로깅 설정"""
        try:
            # 로그 파일명에 타임스탬프 추가
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = os.path.join(self.log_dir, f"tennis_court_monitor_{timestamp}.log")
            
            # 로깅 설정
            logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s - %(levelname)s - %(message)s',
                handlers=[
                    logging.FileHandler(log_file, encoding='utf-8'),
                    logging.StreamHandler()
                ]
            )
            logging.info("로그 설정 완료")
        except Exception as e:
            print(f"❌ 로깅 설정 중 오류 발생: {e}")

    def save_results(self, results):
        """결과를 파일로 저장"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(self.log_dir, f"available_courts_{timestamp}.txt")
            
            with open(filename, 'w', encoding='utf-8') as f:
                f.write("=== 예약 가능한 시간대 ===\n\n")
                
                # 날짜별로 그룹화
                by_date = {}
                for result in results:
                    date = result['date']
                    if date not in by_date:
                        by_date[date] = []
                    by_date[date].append(result)
                
                # 날짜별로 정렬하여 출력
                for date in sorted(by_date.keys()):
                    f.write(f"\n[{date}]\n")
                    f.write("-" * 50 + "\n")
                    
                    # 시설별로 그룹화
                    by_facility = {}
                    for result in by_date[date]:
                        facility = result['facility_name']
                        if facility not in by_facility:
                            by_facility[facility] = []
                        by_facility[facility].append(result)
                    
                    # 시설별로 정렬하여 출력
                    for facility in sorted(by_facility.keys()):
                        f.write(f"\n{facility}\n")
                        for result in sorted(by_facility[facility], key=lambda x: x['time']):
                            f.write(f"  - {result['court']}: {result['time']}\n")
                    
                    f.write("\n")
            
            print(f"💾 결과 저장 완료: {filename}")
        except Exception as e:
            print(f"❌ 결과 저장 중 오류 발생: {e}")

    def load_monitoring_settings(self):
        """모니터링 설정 파일 로드"""
        try:
            if os.path.exists(self.monitoring_file):
                with open(self.monitoring_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                
                current_facility = None
                for line in content.split('\n'):
                    line = line.strip()
                    if not line or line.startswith('//'):
                        continue
                    
                    # 시설 정보 파싱 (예: FAC26(탄천실내))
                    if line.startswith('FAC'):
                        match = re.match(r'(FAC\d+)\(([^)]+)\)', line)
                        if match:
                            facility_id = match.group(1)
                            facility_name = match.group(2)
                            current_facility = facility_id
                            self.facilities.append({
                                'id': facility_id,
                                'name': facility_name,
                                'times': []
                            })
                    # 시간 정보 파싱
                    elif current_facility and ':' in line and '~' in line:
                        time_slot = line.strip()
                        self.facilities[-1]['times'].append(time_slot)
                
                print(f"✅ 모니터링 설정 로드 완료: {len(self.facilities)}개 시설")
                for fac in self.facilities:
                    print(f"   - {fac['id']}({fac['name']}): {len(fac['times'])}개 시간대")
            else:
                print(f"❌ 모니터링 설정 파일 '{self.monitoring_file}'이 존재하지 않습니다.")
        except Exception as e:
            print(f"❌ 모니터링 설정 로드 중 오류 발생: {e}")

    def login(self):
        """로그인 수행"""
        if not self.username or not self.password:
            print("❌ 인증 정보가 없습니다. auth.txt 파일을 확인해주세요.")
            return False
        
        try:
            print("🔐 로그인 시도 중...")
            
            # 로그인 API 호출
            login_api_url = f"{self.base_url}/rest_loginCheck.do"
            login_data = {
                'web_id': self.username,
                'web_pw': self.password
            }
            
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': f"{self.base_url}/login.do"
            }
            
            api_response = self.session.post(login_api_url, data=login_data, headers=headers)
            
            if api_response.status_code == 200:
                response_text = api_response.text.strip()
                
                if response_text == "success":
                    print("✅ 로그인 성공!")
                    return True
                elif response_text == "fail":
                    print("❌ 로그인 실패: 아이디 또는 비밀번호가 잘못되었습니다.")
                elif response_text == "no_id":
                    print("❌ 로그인 실패: 존재하지 않는 아이디입니다.")
                elif response_text == "fail_5":
                    print("❌ 로그인 실패: 5회 이상 비밀번호 오류로 계정이 잠겼습니다.")
                elif response_text == "black_list":
                    print("❌ 로그인 실패: 공공시설예약 이용이 제한된 계정입니다.")
                else:
                    print(f"❌ 알 수 없는 로그인 응답: '{response_text}'")
            else:
                print(f"❌ 로그인 API 요청 실패: HTTP {api_response.status_code}")
            
            return False
                
        except Exception as e:
            print(f"❌ 로그인 중 오류 발생: {e}")
            return False
    
    def get_timetable(self, facility_id, date_str):
        """타임테이블 조회"""
        try:
            # 타임테이블 URL
            url = f"{self.base_url}/otherTimetable.do"
            
            # 날짜 형식 변환 (YYYY-MM-DD -> YYYY-M-D)
            date_parts = date_str.split('-')
            formatted_date = f"{date_parts[0]}-{int(date_parts[1])}-{int(date_parts[2])}"
            
            # 요청 파라미터
            params = {
                'facId': facility_id,
                'resdate': formatted_date
            }
            
            # 디버깅을 위한 URL과 파라미터 출력
            print(f"\n🔍 타임테이블 요청:")
            print(f"URL: {url}")
            print(f"파라미터: {params}")
            
            # 타임테이블 조회
            response = self.session.get(url, params=params)
            
            # 응답 상태 확인
            if response.status_code == 200:
                if 'login.do' in response.url or '로그인' in response.text:
                    print(f"⚠️  세션이 만료되었습니다.")
                    return None
                
                # 디버깅을 위해 HTML 저장
                debug_file = f"log/timetable_raw_{facility_id}_{date_str}.html"
                with open(debug_file, 'w', encoding='utf-8') as f:
                    f.write(response.text)
                print(f"📝 원본 HTML 저장됨: {debug_file}")
                
                return response.text
            else:
                print(f"❌ 타임테이블 조회 실패: HTTP {response.status_code}")
                # 에러 응답 저장
                error_file = f"log/timetable_error_{facility_id}_{date_str}.html"
                with open(error_file, 'w', encoding='utf-8') as f:
                    f.write(response.text)
                print(f"📝 에러 응답 저장됨: {error_file}")
                return None
                
        except Exception as e:
            print(f"❌ 타임테이블 조회 중 오류 발생: {e}")
            return None
    
    def parse_timetable(self, html_content, facility_id, date_str):
        """타임테이블 HTML 파싱"""
        try:
            if not html_content:
                return [], []
            
            available_slots = []
            all_slots = []
            
            # 코트별로 분리 (label 태그와 tableBox 클래스를 사용)
            court_sections = re.findall(r'<label class=\'tit required lb-timetable\'>.*?(\d+)번 코트.*?</label>.*?<div class=\'tableBox mgb30\'.*?<tbody>(.*?)</tbody>', html_content, re.DOTALL)
            
            for court_num, court_content in court_sections:
                # "이용가능한 시간이 없습니다" 체크
                if "이용가능한 시간이 없습니다" in court_content:
                    continue
                
                # 모든 시간대 찾기 (수정된 패턴)
                all_times = re.findall(r'<tr>\s*<td class=\'td-title\'>\s*(.*?)\s*</td>\s*<td class=\'td-title\'>(\d+)</td>\s*<td class=\'td-title\'>(\d{1,2}:\d{2})\s*[~～]\s*(\d{1,2}:\d{2})</td>\s*<td class=\'td-title\'>\s*(.*?)\s*</td>\s*</tr>', court_content)
                
                print(f"\n🔍 코트 {court_num}번 파싱 결과:")
                print(f"- 발견된 전체 시간대 수: {len(all_times)}")
                
                # 모든 시간대 처리
                for time_slot in all_times:
                    button_html = time_slot[0]
                    round_num = time_slot[1]
                    start_time = time_slot[2]
                    end_time = time_slot[3]
                    reservation_name = time_slot[4].strip()
                    
                    # 한 자리 시간을 두 자리로 변환
                    if len(start_time.split(':')[0]) == 1:
                        start_time = '0' + start_time
                    if len(end_time.split(':')[0]) == 1:
                        end_time = '0' + end_time
                    
                    # 예약 상태 확인
                    is_available = '예약가능' in button_html
                    
                    # 시간대 정보 저장
                    slot_info = {
                        'court': f"{court_num}번 코트",
                        'time': f"{start_time} ~ {end_time}",
                        'is_available': is_available,
                        'reservation_name': reservation_name
                    }
                    
                    # 모든 시간대 저장
                    all_slots.append(slot_info)
                    
                    # 예약 가능한 경우 available_slots에도 추가
                    if is_available:
                        available_slots.append(slot_info)
                        print(f"✅ 예약 가능 시간대 발견: {slot_info}")
            
            print(f"\n📊 전체 파싱 결과:")
            print(f"- 전체 시간대 수: {len(all_slots)}")
            print(f"- 예약 가능 시간대 수: {len(available_slots)}")
            
            return available_slots, all_slots
            
        except Exception as e:
            print(f"❌ 타임테이블 파싱 중 오류 발생: {e}")
            return [], []

    def monitor_courts(self):
        """테니스 코트 모니터링 실행"""
        try:
            # 로그인
            if not self.login():
                print("❌ 로그인 실패")
                return [], []
            
            print("\n🔍 테니스 코트 모니터링 시작...")
            
            # 오늘부터 4일간 모니터링
            all_available = []
            all_courts = []
            
            for i in range(4):
                date = datetime.now() + timedelta(days=i)
                date_str = date.strftime('%Y-%m-%d')
                
                print(f"\n📅 {date_str} 모니터링 중...")
                
                # 각 시설별 모니터링
                for facility in self.facilities:
                    facility_id = facility['id']
                    facility_name = facility['name']
                    time_slots = facility['times']
                    
                    print(f"\n🏟️  {facility_name} ({facility_id}) 모니터링")
                    
                    # 타임테이블 조회
                    timetable_html = self.get_timetable(facility_id, date_str)
                    if timetable_html:
                        # 예약 가능한 시간대 파싱
                        available_slots, all_slots = self.parse_timetable(timetable_html, facility_id, date_str)
                        
                        # 모든 코트 정보 저장
                        for slot in all_slots:
                            court_info = {
                                'date': date_str,
                                'facility_name': facility_name,
                                'facility_id': facility_id,
                                'court': slot['court'],
                                'time': slot['time'],
                                'is_available': slot['is_available'],
                                'reservation_name': slot['reservation_name']
                            }
                            all_courts.append(court_info)
                        
                        # 모니터링 설정된 시간대와 비교
                        for slot in available_slots:
                            slot_time = slot['time']
                            for target_time in time_slots:
                                if self.time_ranges_match(slot_time, target_time):
                                    all_available.append({
                                        'facility_name': facility_name,
                                        'facility_id': facility_id,
                                        'date': date_str,
                                        'time': slot_time,
                                        'court': slot['court']
                                    })
            
            # 결과 출력
            if all_available:
                print("\n✅ 예약 가능한 시간대:")
                for available in all_available:
                    print(f"   - {available['facility_name']} {available['court']} - {available['date']} {available['time']}")
            else:
                print("\n❌ 예약 가능한 시간대가 없습니다.")
            
            print(f"\n📊 전체 코트 수: {len(all_courts)}")
            
            # 결과를 파일로 저장
            self.save_results(all_available)
            
            return all_available, all_courts
            
        except Exception as e:
            print(f"❌ 모니터링 중 오류 발생: {e}")
            return [], []

    def time_ranges_match(self, slot_time, target_time):
        """시간 범위가 일치하는지 확인"""
        try:
            # 시간 범위 정규화
            slot_time = slot_time.replace('～', '~').strip()
            target_time = target_time.replace('～', '~').strip()
            
            # 시작 시간과 종료 시간 분리
            slot_start, slot_end = [t.strip() for t in slot_time.split('~')]
            target_start, target_end = [t.strip() for t in target_time.split('~')]
            
            # 시간이 일치하는지 확인
            return slot_start == target_start and slot_end == target_end
            
        except Exception:
            return False

def create_templates_dir():
    """템플릿 디렉토리 생성"""
    templates_dir = os.path.join(os.path.dirname(__file__), 'templates')
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)

def create_static_dir():
    """정적 파일 디렉토리 생성"""
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)

def create_html_template():
    """HTML 템플릿 파일 생성"""
    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'index.html')
    if not os.path.exists(template_path):
        with open(template_path, 'w', encoding='utf-8') as f:
            f.write('''<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>테니스 코트 예약 현황</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {
            font-family: 'Arial', sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #1a1a1a;
            color: #e0e0e0;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        .header {
            background-color: #2d2d2d;
            color: #ffffff;
            padding: 20px;
            border-radius: 5px;
            margin-bottom: 20px;
            text-align: center;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }
        .section {
            background-color: #2d2d2d;
            padding: 20px;
            border-radius: 5px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }
        .section h2 {
            color: #ffffff;
            margin-top: 0;
            border-bottom: 2px solid #4CAF50;
            padding-bottom: 10px;
        }
        .court-list {
            list-style: none;
            padding: 0;
        }
        .court-item {
            background-color: #363636;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 5px;
            border-left: 5px solid #4CAF50;
        }
        .court-item.reserved {
            background-color: #3d2b2b;
            border-left-color: #f44336;
        }
        .court-item.available {
            background-color: #2b3d2b;
            border-left-color: #4CAF50;
        }
        .last-update {
            text-align: right;
            color: #888;
            font-size: 0.9em;
            margin-top: 10px;
        }
        .status-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            background-color: #363636;
        }
        .status-table th, .status-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #444;
        }
        .status-table th {
            background-color: #2d2d2d;
            font-weight: bold;
            color: #ffffff;
        }
        .status-table tr:hover {
            background-color: #404040;
        }
        .status-available {
            color: #4CAF50;
            font-weight: bold;
        }
        .status-reserved {
            color: #f44336;
        }
        .facility-section {
            margin-bottom: 20px;
        }
        .facility-header {
            background-color: #363636;
            padding: 10px;
            cursor: pointer;
            border-radius: 4px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: #ffffff;
        }
        .facility-header:hover {
            background-color: #404040;
        }
        .date-section {
            margin: 10px 0;
        }
        .date-header {
            background-color: #404040;
            padding: 8px;
            cursor: pointer;
            border-radius: 4px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: #ffffff;
        }
        .date-header:hover {
            background-color: #4a4a4a;
        }
        .toggle-icon {
            font-size: 12px;
            color: #888;
        }
        .facility-content, .date-content {
            padding: 10px;
            background-color: #363636;
            border-radius: 0 0 4px 4px;
        }
        .alert {
            padding: 15px;
            margin-bottom: 20px;
            border-radius: 4px;
        }
        .alert-info {
            background-color: #2b3d2b;
            color: #4CAF50;
            border: 1px solid #4CAF50;
        }
        .alert-danger {
            background-color: #3d2b2b;
            color: #f44336;
            border: 1px solid #f44336;
        }
        .spinner-border {
            color: #4CAF50;
        }
        .btn-primary {
            background-color: #4CAF50;
            border-color: #4CAF50;
            color: #ffffff;
        }
        .btn-primary:hover {
            background-color: #45a049;
            border-color: #45a049;
        }
        .refresh-time {
            color: #888;
        }
        @media (max-width: 768px) {
            .container {
                padding: 10px;
            }
            .status-table {
                font-size: 0.8em;
            }
            .section {
                padding: 15px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h1>테니스 코트 예약 현황</h1>
            <div>
                <span class="refresh-time me-3">마지막 갱신: <span id="lastUpdate">-</span></span>
                <button class="btn btn-primary" onclick="refreshData()">새로고침</button>
            </div>
        </div>
        
        <div id="results">
            <div class="text-center">
                <div class="spinner-border" role="status">
                    <span class="visually-hidden">Loading...</span>
                </div>
            </div>
        </div>

        <div class="mt-4">
            <h3>전체 코트 현황</h3>
            <div id="statusTable">
                <div class="text-center">
                    <div class="spinner-border" role="status">
                        <span class="visually-hidden">Loading...</span>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        function formatDate(dateStr) {
            const date = new Date(dateStr);
            return date.toLocaleString('ko-KR', {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            });
        }

        function refreshData() {
            document.getElementById('results').innerHTML = `
                <div class="text-center">
                    <div class="spinner-border" role="status">
                        <span class="visually-hidden">Loading...</span>
                    </div>
                </div>
            `;
            document.getElementById('statusTable').innerHTML = `
                <div class="text-center">
                    <div class="spinner-border" role="status">
                        <span class="visually-hidden">Loading...</span>
                    </div>
                </div>
            `;
            fetch('/get_results')
                .then(response => response.json())
                .then(data => {
                    updateResults(data);
                    updateStatusTable(data);
                })
                .catch(error => {
                    console.error('Error:', error);
                    document.getElementById('results').innerHTML = `
                        <div class="alert alert-danger">
                            데이터를 불러오는 중 오류가 발생했습니다.
                        </div>
                    `;
                });
        }

        function updateResults(data) {
            const resultsDiv = document.getElementById('results');
            document.getElementById('lastUpdate').textContent = formatDate(data.last_update);
            
            if (data.results.length === 0) {
                resultsDiv.innerHTML = '<div class="alert alert-info">예약 가능한 시간대가 없습니다.</div>';
                return;
            }

            let html = '';
            const byDate = {};
            
            // 날짜별로 그룹화
            data.results.forEach(result => {
                if (!byDate[result.date]) {
                    byDate[result.date] = [];
                }
                byDate[result.date].push(result);
            });

            // 날짜별로 정렬하여 출력
            Object.keys(byDate).sort().forEach(date => {
                html += `<div class="card mb-4">
                    <div class="card-header">
                        <h5 class="mb-0">${date}</h5>
                    </div>
                    <div class="card-body">`;
                
                // 시설별로 그룹화
                const byFacility = {};
                byDate[date].forEach(result => {
                    if (!byFacility[result.facility_name]) {
                        byFacility[result.facility_name] = [];
                    }
                    byFacility[result.facility_name].push(result);
                });

                // 시설별로 정렬하여 출력
                Object.keys(byFacility).sort().forEach(facility => {
                    html += `<div class="court-info">
                        <h6 class="mb-2">${facility}</h6>
                        <div class="ms-3">`;
                    
                    byFacility[facility].sort((a, b) => a.time.localeCompare(b.time))
                        .forEach(result => {
                            html += `<div class="available">${result.court}: ${result.time}</div>`;
                        });
                    
                    html += `</div></div>`;
                });

                html += `</div></div>`;
            });

            resultsDiv.innerHTML = html;
        }

        function updateStatusTable(data) {
            const tableDiv = document.getElementById('statusTable');
            
            if (!data.all_courts || data.all_courts.length === 0) {
                tableDiv.innerHTML = '<div class="alert alert-info">코트 정보가 없습니다.</div>';
                return;
            }

            let html = '<div class="table-responsive"><table class="table table-bordered status-table">';
            
            // 테이블 헤더
            html += '<thead><tr>';
            html += '<th>날짜</th>';
            html += '<th>시설</th>';
            html += '<th>코트</th>';
            html += '<th>시간</th>';
            html += '<th>상태</th>';
            html += '</tr></thead><tbody>';

            // 테이블 내용
            data.all_courts.forEach(court => {
                const isAvailable = data.results.some(r => 
                    r.date === court.date && 
                    r.facility_name === court.facility_name && 
                    r.court === court.court && 
                    r.time === court.time
                );

                html += `<tr>
                    <td>${court.date}</td>
                    <td>${court.facility_name}</td>
                    <td>${court.court}</td>
                    <td>${court.time}</td>
                    <td class="${isAvailable ? 'available' : 'unavailable'}">
                        ${isAvailable ? '예약 가능한 코트' : '예약 불가'}
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            tableDiv.innerHTML = html;
        }

        // 페이지 로드 시 데이터 가져오기
        refreshData();
        
        // 1분마다 자동 갱신
        setInterval(refreshData, 60000);
    </script>
</body>
</html>''')

def create_css_file():
    """CSS 파일 생성"""
    css_path = os.path.join(os.path.dirname(__file__), 'static', 'style.css')
    if not os.path.exists(css_path):
        with open(css_path, 'w', encoding='utf-8') as f:
            f.write('''/* 추가 스타일이 필요한 경우 여기에 작성 */''')

@app.route('/')
def index():
    """메인 페이지"""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>테니스 코트 모니터링</title>
        <style>
            body {
                font-family: 'Arial', sans-serif;
                margin: 0;
                padding: 20px;
                background-color: #1a1a1a;
                color: #e0e0e0;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
            }
            .header {
                background-color: #2d2d2d;
                color: #ffffff;
                padding: 20px;
                border-radius: 5px;
                margin-bottom: 20px;
                text-align: center;
                box-shadow: 0 2px 4px rgba(0,0,0,0.2);
            }
            .section {
                background-color: #2d2d2d;
                padding: 20px;
                border-radius: 5px;
                margin-bottom: 20px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.2);
            }
            .section h2 {
                color: #ffffff;
                margin-top: 0;
                border-bottom: 2px solid #4CAF50;
                padding-bottom: 10px;
            }
            .court-list {
                list-style: none;
                padding: 0;
            }
            .court-item {
                background-color: #363636;
                padding: 15px;
                margin-bottom: 10px;
                border-radius: 5px;
                border-left: 5px solid #4CAF50;
            }
            .court-item.reserved {
                background-color: #3d2b2b;
                border-left-color: #f44336;
            }
            .court-item.available {
                background-color: #2b3d2b;
                border-left-color: #4CAF50;
            }
            .last-update {
                text-align: right;
                color: #888;
                font-size: 0.9em;
                margin-top: 10px;
            }
            .status-table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 20px;
                background-color: #363636;
            }
            .status-table th, .status-table td {
                padding: 12px;
                text-align: left;
                border-bottom: 1px solid #444;
            }
            .status-table th {
                background-color: #2d2d2d;
                font-weight: bold;
                color: #ffffff;
            }
            .status-table tr:hover {
                background-color: #404040;
            }
            .status-available {
                color: #4CAF50;
                font-weight: bold;
            }
            .status-reserved {
                color: #f44336;
            }
            .facility-section {
                margin-bottom: 20px;
            }
            .facility-header {
                background-color: #363636;
                padding: 10px;
                cursor: pointer;
                border-radius: 4px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                color: #ffffff;
            }
            .facility-header:hover {
                background-color: #404040;
            }
            .date-section {
                margin: 10px 0;
            }
            .date-header {
                background-color: #404040;
                padding: 8px;
                cursor: pointer;
                border-radius: 4px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                color: #ffffff;
            }
            .date-header:hover {
                background-color: #4a4a4a;
            }
            .toggle-icon {
                font-size: 12px;
                color: #888;
            }
            .facility-content, .date-content {
                padding: 10px;
                background-color: #363636;
                border-radius: 0 0 4px 4px;
            }
            .alert {
                padding: 15px;
                margin-bottom: 20px;
                border-radius: 4px;
            }
            .alert-info {
                background-color: #2b3d2b;
                color: #4CAF50;
                border: 1px solid #4CAF50;
            }
            .alert-danger {
                background-color: #3d2b2b;
                color: #f44336;
                border: 1px solid #f44336;
            }
            .spinner-border {
                color: #4CAF50;
            }
            .btn-primary {
                background-color: #4CAF50;
                border-color: #4CAF50;
                color: #ffffff;
            }
            .btn-primary:hover {
                background-color: #45a049;
                border-color: #45a049;
            }
            .refresh-time {
                color: #888;
            }
            @media (max-width: 768px) {
                .container {
                    padding: 10px;
                }
                .status-table {
                    font-size: 0.8em;
                }
                .section {
                    padding: 15px;
                }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>테니스 코트 모니터링</h1>
            </div>
            
            <div class="section">
                <h2>예약 가능한 관심 코트</h2>
                <div id="available-courts">
                    <p>로딩 중...</p>
                </div>
                <div class="last-update" id="last-update"></div>
            </div>
            
            <div class="section">
                <h2>예약 가능한 모든 코트</h2>
                <div id="all-available-courts">
                    <p>로딩 중...</p>
                </div>
            </div>
            
            <div class="section">
                <h2>전체 코트 현황</h2>
                <div id="all-courts">
                    <p>로딩 중...</p>
                </div>
            </div>
        </div>

        <script>
            function updateResults() {
                fetch('/get_results')
                    .then(response => response.json())
                    .then(data => {
                        // 예약 가능한 코트 표시
                        const availableCourts = document.getElementById('available-courts');
                        if (data.results && data.results.length > 0) {
                            availableCourts.innerHTML = '<ul class="court-list">' +
                                data.results.map(court => `
                                    <li class="court-item available">
                                        ${court.facility_name} ${court.court} - ${court.date} ${court.time}
                                    </li>
                                `).join('') + '</ul>';
                        } else {
                            availableCourts.innerHTML = '<p>예약 가능한 코트가 없습니다.</p>';
                        }
                        
                        // 예약 가능한 모든 코트 표시
                        const allAvailableCourts = document.getElementById('all-available-courts');
                        if (data.all_courts && data.all_courts.length > 0) {
                            const availableCourts = data.all_courts.filter(court => court.is_available);
                            if (availableCourts.length > 0) {
                                // 시설별로 그룹화
                                const facilities = {};
                                availableCourts.forEach(court => {
                                    if (!facilities[court.facility_name]) {
                                        facilities[court.facility_name] = {};
                                    }
                                    if (!facilities[court.facility_name][court.date]) {
                                        facilities[court.facility_name][court.date] = [];
                                    }
                                    facilities[court.facility_name][court.date].push(court);
                                });
                                
                                // HTML 생성
                                let html = '';
                                for (const [facility, dates] of Object.entries(facilities)) {
                                    html += `
                                        <div class="facility-section">
                                            <div class="facility-header" onclick="toggleSection(this)">
                                                <span>${facility}</span>
                                                <span class="toggle-icon">▲</span>
                                            </div>
                                            <div class="facility-content" style="display: block">
                                    `;
                                    
                                    for (const [date, courts] of Object.entries(dates)) {
                                        html += `
                                            <div class="date-section">
                                                <div class="date-header" onclick="toggleSection(this)">
                                                    <span>${date}</span>
                                                    <span class="toggle-icon">▲</span>
                                                </div>
                                                <div class="date-content" style="display: block">
                                                    <table class="status-table">
                                                        <thead>
                                                            <tr>
                                                                <th>코트</th>
                                                                <th>시간</th>
                                                                <th>상태</th>
                                                            </tr>
                                                        </thead>
                                                        <tbody>
                                                            ${courts.map(court => `
                                                                <tr>
                                                                    <td>${court.court}</td>
                                                                    <td>${court.time}</td>
                                                                    <td class="status-available">예약 가능</td>
                                                                </tr>
                                                            `).join('')}
                                                        </tbody>
                                                    </table>
                                                </div>
                                            </div>
                                        `;
                                    }
                                    
                                    html += `
                                        </div>
                                    </div>
                                `;
                                }
                                
                                allAvailableCourts.innerHTML = html;
                            } else {
                                allAvailableCourts.innerHTML = '<p>예약 가능한 코트가 없습니다.</p>';
                            }
                        } else {
                            allAvailableCourts.innerHTML = '<p>코트 정보를 불러올 수 없습니다.</p>';
                        }
                        
                        // 전체 코트 현황 표시
                        const allCourts = document.getElementById('all-courts');
                        if (data.all_courts && data.all_courts.length > 0) {
                            // 시설별로 그룹화
                            const facilities = {};
                            data.all_courts.forEach(court => {
                                if (!facilities[court.facility_name]) {
                                    facilities[court.facility_name] = {};
                                }
                                if (!facilities[court.facility_name][court.date]) {
                                    facilities[court.facility_name][court.date] = [];
                                }
                                facilities[court.facility_name][court.date].push(court);
                            });
                            
                            // HTML 생성
                            let html = '';
                            for (const [facility, dates] of Object.entries(facilities)) {
                                // 시설에 예약 가능한 코트가 있는지 확인
                                const hasAvailableCourts = Object.values(dates).some(courts => 
                                    courts.some(court => court.is_available)
                                );
                                
                                html += `
                                    <div class="facility-section">
                                        <div class="facility-header" onclick="toggleSection(this)">
                                            <span>${facility}</span>
                                            <span class="toggle-icon">${hasAvailableCourts ? '▲' : '▼'}</span>
                                        </div>
                                        <div class="facility-content" style="display: ${hasAvailableCourts ? 'block' : 'none'}">
                                `;
                                
                                for (const [date, courts] of Object.entries(dates)) {
                                    // 해당 날짜에 예약 가능한 코트가 있는지 확인
                                    const hasAvailableCourts = courts.some(court => court.is_available);
                                    
                                    html += `
                                        <div class="date-section">
                                            <div class="date-header" onclick="toggleSection(this)">
                                                <span>${date}</span>
                                                <span class="toggle-icon">${hasAvailableCourts ? '▲' : '▼'}</span>
                                            </div>
                                            <div class="date-content" style="display: ${hasAvailableCourts ? 'block' : 'none'}">
                                                <table class="status-table">
                                                    <thead>
                                                        <tr>
                                                            <th>코트</th>
                                                            <th>시간</th>
                                                            <th>상태</th>
                                                        </tr>
                                                    </thead>
                                                    <tbody>
                                                        ${courts.map(court => `
                                                            <tr>
                                                                <td>${court.court}</td>
                                                                <td>${court.time}</td>
                                                                <td class="${court.is_available ? 'status-available' : 'status-reserved'}">
                                                                    ${court.is_available ? '예약 가능' : (court.reservation_name ? `${court.reservation_name} 님 예약` : '예약됨')}
                                                                </td>
                                                            </tr>
                                                        `).join('')}
                                                    </tbody>
                                                </table>
                                            </div>
                                        </div>
                                    `;
                                }
                                
                                html += `
                                        </div>
                                    </div>
                                `;
                            }
                            
                            allCourts.innerHTML = html;
                        } else {
                            allCourts.innerHTML = '<p>코트 정보를 불러올 수 없습니다.</p>';
                        }
                        
                        // 마지막 업데이트 시간 표시
                        document.getElementById('last-update').textContent = 
                            '마지막 업데이트: ' + data.last_update;
                    })
                    .catch(error => {
                        console.error('Error:', error);
                        document.getElementById('available-courts').innerHTML = 
                            '<p>데이터를 불러오는 중 오류가 발생했습니다.</p>';
                        document.getElementById('all-courts').innerHTML = 
                            '<p>데이터를 불러오는 중 오류가 발생했습니다.</p>';
                    });
            }

            function toggleSection(element) {
                const content = element.nextElementSibling;
                const icon = element.querySelector('.toggle-icon');
                
                if (content.style.display === 'block') {
                    content.style.display = 'none';
                    icon.textContent = '▼';
                } else {
                    content.style.display = 'block';
                    icon.textContent = '▲';
                }
            }

            // 페이지 로드 시 첫 업데이트
            updateResults();
            
            // 1분마다 자동 업데이트
            setInterval(updateResults, 60000);
        </script>
    </body>
    </html>
    '''

@app.route('/get_results')
def get_results():
    """현재 모니터링 결과 반환"""
    global monitoring_results, scheduler
    available_results, all_courts = scheduler.monitor_courts()
    
    print(f"\n📊 API 응답 - 예약 가능한 코트 수: {len(available_results)}")
    print(f"📊 API 응답 - 전체 코트 수: {len(all_courts)}")
    
    # 결과를 날짜, 시설, 코트, 시간 순으로 정렬
    all_courts.sort(key=lambda x: (x['date'], x['facility_name'], x['court'], x['time']))
    
    response_data = {
        'results': available_results,
        'all_courts': all_courts,
        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    return jsonify(response_data)

def run_flask():
    """Flask 서버 실행"""
    app.run(host='0.0.0.0', port=5000, debug=False)

def main():
    try:
        # 인증 정보 로드
        username = None
        password = None
        
        # auth.txt 파일에서 인증 정보 로드 시도
        if os.path.exists("auth.txt"):
            with open("auth.txt", 'r', encoding='utf-8') as f:
                lines = f.read().strip().split('\n')
                if len(lines) >= 2:
                    username = lines[0].strip()
                    password = lines[1].strip()
        
        # auth.txt 파일이 없거나 형식이 잘못된 경우 환경 변수에서 로드
        if not username or not password:
            username = os.environ.get("WebId")
            password = os.environ.get("WebPassword")
            
            if not username or not password:
                print("❌ 인증 정보를 찾을 수 없습니다. auth.txt 파일 또는 환경 변수(WebId, WebPassword)를 확인해주세요.")
                return
        
        # 웹 인터페이스 설정
        create_templates_dir()
        create_static_dir()
        create_html_template()
        create_css_file()
        
        # 스케줄러 초기화
        global scheduler
        scheduler = TennisCourtScheduler(username, password)
        
        # Flask 서버를 별도 스레드로 실행
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()
        
        print("🌐 웹 서버가 시작되었습니다. http://localhost:5000 에서 확인하세요.")
        
        # 모니터링 실행
        while True:
            try:
                # 모니터링 실행
                results, all_courts = scheduler.monitor_courts()
                
                # 결과 업데이트
                global monitoring_results
                monitoring_results = results
                
                # 1분 대기
                time.sleep(60)
                
            except Exception as e:
                print(f"❌ 모니터링 중 오류 발생: {e}")
                time.sleep(60)  # 오류 발생 시 1분 대기
        
    except Exception as e:
        print(f"❌ 프로그램 실행 중 오류 발생: {e}")

if __name__ == "__main__":
    main()
