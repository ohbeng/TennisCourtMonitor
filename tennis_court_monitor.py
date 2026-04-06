#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
테니스 코트 예약 현황 모니터링 스크립트
MonitoringTable.txt에 정의된 시설과 시간대를 기반으로
오늘부터 3일 후까지 예약 가능한 코트를 확인합니다.
"""

import sys
import io

# Windows 터미널 UTF-8 인코딩 설정 (이모지 출력 지원)
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import requests
from bs4 import BeautifulSoup
import os
from datetime import datetime, timedelta, timezone
import time
import urllib3
import re
import logging
import shutil
from flask import Flask, render_template, jsonify
import threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
import json

# SSL 경고 메시지 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
scheduler = None
monitoring_results = []
all_courts_cache = []  # 전체 코트 정보 캐시
last_update_time = None  # 마지막 업데이트 시간
last_email_sent = {}  # 이메일 전송 기록을 저장
last_available_courts = {}  # 이전 예약 가능한 코트 정보를 저장
cache_lock = threading.Lock()  # 캐시 접근 동기화를 위한 Lock

KST = timezone(timedelta(hours=9))

class TennisCourtScheduler:
    def __init__(self, accounts, monitoring_file="MonitoringTable.txt"):
        self.accounts = accounts  # 계정 정보 리스트 [{'username': 'user1', 'password': 'pass1'}, ...]
        self.current_account_index = 0  # 현재 사용 중인 계정 인덱스
        self.base_url = "https://res.isdc.co.kr"
        self.session = requests.Session()
        # SSL 인증서 검증 비활성화
        self.session.verify = False
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
            one_day_ago = current_time - (1 * 24 * 60 * 60)  # 1일을 초로 변환
            
            for filename in os.listdir(self.log_dir):
                filepath = os.path.join(self.log_dir, filename)
                if os.path.isfile(filepath):
                    file_time = os.path.getmtime(filepath)
                    if file_time < one_day_ago:
                        os.remove(filepath)
                        print(f"🗑️ 오래된 파일 삭제: {filename}")
        except Exception as e:
            print(f"❌ 파일 정리 중 오류 발생: {e}")

    def setup_logging(self):
        """로깅 설정"""
        try:
            # 로그 파일명에 타임스탬프 추가
            timestamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
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
            timestamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
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
                current_section = 'weekday'  # 기본값: 주중
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
                            current_section = 'weekday'
                            self.facilities.append({
                                'id': facility_id,
                                'name': facility_name,
                                'weekday_times': [],
                                'weekend_times': []
                            })
                    # 주중/주말 섹션 구분
                    elif line == '주중':
                        current_section = 'weekday'
                    elif line == '주말':
                        current_section = 'weekend'
                    # All 키워드: 해당 구간의 모든 시간대 모니터링
                    elif current_facility and line.lower() == 'all':
                        if current_section == 'weekday':
                            self.facilities[-1]['weekday_times'] = ['ALL']
                        else:
                            self.facilities[-1]['weekend_times'] = ['ALL']
                    # 시간 정보 파싱 (#으로 시작하는 비활성 시간대 제외)
                    elif current_facility and ':' in line and '~' in line and not line.startswith('#'):
                        time_slot = line.strip()
                        if current_section == 'weekday':
                            self.facilities[-1]['weekday_times'].append(time_slot)
                        else:
                            self.facilities[-1]['weekend_times'].append(time_slot)
                
                print(f"✅ 모니터링 설정 로드 완료: {len(self.facilities)}개 시설")
                for fac in self.facilities:
                    print(f"   - {fac['id']}({fac['name']}): 주중 {len(fac['weekday_times'])}개, 주말 {len(fac['weekend_times'])}개 시간대")
            else:
                print(f"❌ 모니터링 설정 파일 '{self.monitoring_file}'이 존재하지 않습니다.")
        except Exception as e:
            print(f"❌ 모니터링 설정 로드 중 오류 발생: {e}")

    def get_current_account(self):
        """현재 사용할 계정 정보 반환"""
        if not self.accounts:
            return None, None
        return self.accounts[self.current_account_index]['username'], self.accounts[self.current_account_index]['password']
    
    def switch_to_next_account(self):
        """다음 계정으로 전환"""
        if len(self.accounts) > 1:
            self.current_account_index = (self.current_account_index + 1) % len(self.accounts)
            print(f"🔄 계정 전환: {self.current_account_index + 1}번째 계정으로 변경")
    
    def login(self):
        """로그인 수행 - 실패 시 다음 계정으로 자동 전환"""
        if not self.accounts:
            print("❌ 인증 정보가 없습니다. auth.txt 파일을 확인해주세요.")
            return False
        
        # 모든 계정에 대해 로그인 시도
        attempts = 0
        max_attempts = len(self.accounts)
        
        while attempts < max_attempts:
            username, password = self.get_current_account()
            
            try:
                print(f"🔐 로그인 시도 중... ({self.current_account_index + 1}/{len(self.accounts)}번째 계정: {username})")
                
                # 새 세션 생성 (계정 전환 시 세션 초기화)
                self.session = requests.Session()
                # SSL 인증서 검증 비활성화
                self.session.verify = False
                
                # 로그인 API 호출
                login_api_url = f"{self.base_url}/rest_loginCheck.do"
                login_data = {
                    'web_id': username,
                    'web_pw': password
                }
                
                headers = {
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': f"{self.base_url}/login.do"
                }
                
                # SSL 인증서 검증 무시하고 요청
                api_response = self.session.post(login_api_url, data=login_data, headers=headers, verify=False)
                
                if api_response.status_code == 200:
                    response_text = api_response.text.strip()
                    
                    if response_text == "success":
                        print(f"✅ 로그인 성공! ({self.current_account_index + 1}번째 계정: {username})")
                        return True
                    elif response_text == "fail":
                        print(f"❌ 로그인 실패: 아이디 또는 비밀번호가 잘못되었습니다. ({username})")
                    elif response_text == "no_id":
                        print(f"❌ 로그인 실패: 존재하지 않는 아이디입니다. ({username})")
                    elif response_text == "fail_5":
                        print(f"❌ 로그인 실패: 5회 이상 비밀번호 오류로 계정이 잠겼습니다. ({username})")
                    elif response_text == "black_list":
                        print(f"❌ 로그인 실패: 공공시설예약 이용이 제한된 계정입니다. ({username})")
                    else:
                        print(f"❌ 알 수 없는 로그인 응답: '{response_text}' ({username})")
                else:
                    print(f"❌ 로그인 API 요청 실패: HTTP {api_response.status_code} ({username})")
                
            except Exception as e:
                print(f"❌ 로그인 중 오류 발생: {e} ({username})")
            
            # 다음 계정으로 전환
            attempts += 1
            if attempts < max_attempts:
                self.switch_to_next_account()
                time.sleep(1)  # 계정 전환 간 잠시 대기
        
        print("❌ 모든 계정에서 로그인 실패")
        return False
    
    def get_timetable_with_retry(self, facility_id, date_str, max_retries=2):
        """타임테이블 조회 (세션 만료 시 재로그인 처리)"""
        for attempt in range(max_retries):
            timetable_html = self.get_timetable(facility_id, date_str)
            
            if timetable_html is None:
                print(f"⚠️  타임테이블 조회 실패 (시도 {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    print("🔄 다음 계정으로 전환하여 재시도...")
                    self.switch_to_next_account()
                    if not self.login():
                        print("❌ 재로그인 실패")
                        continue
                    time.sleep(1)  # 잠시 대기
                continue
            
            # 세션 만료 체크
            if 'login.do' in timetable_html or '로그인' in timetable_html:
                print(f"⚠️  세션 만료 감지 (시도 {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    print("🔄 다음 계정으로 전환하여 재로그인...")
                    self.switch_to_next_account()
                    if not self.login():
                        print("❌ 재로그인 실패")
                        continue
                    time.sleep(1)  # 잠시 대기
                continue
            
            return timetable_html
        
        print(f"❌ {max_retries}번 시도 후 타임테이블 조회 실패")
        return None
    
    def get_timetable(self, facility_id, date_str):
        """타임테이블 조회"""
        try:
            # 타임테이블 URL (POST 방식으로 변경)
            url = f"{self.base_url}/otherTimetable.do"
            
            # 날짜 형식 변환 (YYYY-MM-DD -> YYYY-M-D)
            date_parts = date_str.split('-')
            formatted_date = f"{date_parts[0]}-{int(date_parts[1])}-{int(date_parts[2])}"
            
            # 요청 파라미터 (POST body)
            data = {
                'facId': facility_id,
                'resdate': formatted_date
            }
            
            # POST 요청에 필요한 헤더 설정
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Origin': self.base_url,
                'Referer': f'{self.base_url}/reservationInfo.do',
                'Sec-Fetch-Site': 'same-origin',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Dest': 'document',
                'Upgrade-Insecure-Requests': '1',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
                'Cache-Control': 'max-age=0'
            }
            
            # 디버깅을 위한 URL과 파라미터 출력
            print(f"\n🔍 타임테이블 요청 (POST):")
            print(f"URL: {url}")
            print(f"Data: {data}")
            
            # 타임테이블 조회 (POST 방식)
            response = self.session.post(url, data=data, headers=headers, verify=False)
            
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
            # 패턴: "1번 코트", "1번코트", "(일요일)1번 코트" 등 모두 매칭
            court_sections = re.findall(r'<label class=\'tit required lb-timetable\'>.*?(\d+)번\s*코트.*?</label>.*?<div class=\'tableBox mgb30\'.*?<tbody>(.*?)</tbody>', html_content, re.DOTALL)
            
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
            
            # 성공/실패 통계
            total_requests = 0
            successful_requests = 0
            failed_requests = 0
            
            for i in range(4):
                date = datetime.now(KST) + timedelta(days=i)
                date_str = date.strftime('%Y-%m-%d')
                
                time.sleep(0.1) # 요청 간 잠시 대기
                print(f"\n📅 {date_str} 모니터링 중...")
                
                # 각 시설별 모니터링
                for facility in self.facilities:
                    facility_id = facility['id']
                    facility_name = facility['name']
                    # 날짜가 주말(토=5, 일=6)이면 주말 시간대, 아니면 주중 시간대 사용
                    if date.weekday() >= 5:
                        time_slots = facility['weekend_times']
                    else:
                        time_slots = facility['weekday_times']
                    
                    time.sleep(0.1) # 요청 간 잠시 대기
                    print(f"\n🏟️  {facility_name} ({facility_id}) 모니터링")
                    total_requests += 1
                    
                    try:
                        # 타임테이블 조회 시 세션 만료 체크 및 재로그인 처리
                        timetable_html = self.get_timetable_with_retry(facility_id, date_str)
                        
                        # HTML이 없으면 저장된 로그 파일에서 읽기 시도
                        if not timetable_html:
                            log_file = f"log/timetable_raw_{facility_id}_{date_str}.html"
                            if os.path.exists(log_file):
                                print(f"📂 로그 파일에서 데이터 로드 시도: {log_file}")
                                try:
                                    with open(log_file, 'r', encoding='utf-8') as f:
                                        timetable_html = f.read()
                                    print(f"✅ 로그 파일에서 데이터 로드 성공")
                                except Exception as e:
                                    print(f"⚠️  로그 파일 읽기 실패: {e}")
                        
                        if timetable_html:
                            # 예약 가능한 시간대 파싱
                            available_slots, all_slots = self.parse_timetable(timetable_html, facility_id, date_str)
                            
                            # 모든 코트 정보 저장 (중복 방지를 위해 고유 키 생성)
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
                                # 중복 방지: 고유 키로 체크하지 않고 무조건 추가 (리스트에 모든 데이터 포함)
                                all_courts.append(court_info)
                            
                            # 모니터링 설정된 시간대와 비교
                            for slot in available_slots:
                                slot_time = slot['time']
                                if 'ALL' in time_slots:
                                    all_available.append({
                                        'facility_name': facility_name,
                                        'facility_id': facility_id,
                                        'date': date_str,
                                        'time': slot_time,
                                        'court': slot['court']
                                    })
                                else:
                                    for target_time in time_slots:
                                        if self.time_ranges_match(slot_time, target_time):
                                            all_available.append({
                                                'facility_name': facility_name,
                                                'facility_id': facility_id,
                                                'date': date_str,
                                                'time': slot_time,
                                                'court': slot['court']
                                            })
                            
                            successful_requests += 1
                            print(f"✅ {facility_name} 조회 성공 - 예약 가능: {len(available_slots)}개, 전체: {len(all_slots)}개")
                        else:
                            failed_requests += 1
                            print(f"⚠️  {facility_name} 타임테이블 조회 및 로그 로드 실패 - 건너뛰고 계속 진행")
                            # 실패해도 계속 진행
                            continue
                            
                    except Exception as e:
                        failed_requests += 1
                        print(f"⚠️  {facility_name} 모니터링 중 오류 발생: {e} - 건너뛰고 계속 진행")
                        # 개별 시설 오류가 발생해도 계속 진행
                        continue
            
            # 모니터링 결과 요약
            print(f"\n📊 모니터링 완료 요약:")
            print(f"   - 전체 요청: {total_requests}개")
            print(f"   - 성공: {successful_requests}개")
            print(f"   - 실패: {failed_requests}개")
            if total_requests > 0:
                print(f"   - 성공률: {(successful_requests/total_requests*100):.1f}%")
            
            # 결과 출력
            if all_available:
                print("\n✅ 예약 가능한 시간대:")
                for available in all_available:
                    print(f"   - {available['facility_name']} {available['court']} - {available['date']} {available['time']}")
            else:
                print("\n❌ 예약 가능한 시간대가 없습니다.")
            
            print(f"\n📊 전체 코트 수: {len(all_courts)}")
            
            # 부분적으로라도 성공한 경우 결과를 파일로 저장
            if successful_requests > 0:
                self.save_results(all_available)
                if failed_requests > 0:
                    print(f"💾 부분 성공 결과 저장 완료 ({successful_requests}/{total_requests} 성공)")
                else:
                    print(f"💾 결과 저장 완료 (모두 성공)")
            elif failed_requests > 0:
                print(f"⚠️  모든 요청 실패 - 결과 저장 생략")
            
            # 실패가 있어도 성공한 데이터는 반환
            return all_available, all_courts
            
        except Exception as e:
            print(f"❌ 모니터링 중 치명적 오류 발생: {e}")
            # 치명적 오류가 발생해도 빈 리스트라도 반환하여 프로그램이 계속 실행되도록 함
            import traceback
            traceback.print_exc()
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

            // 시설별/날짜별 그룹화
            const groupedData = {};
            data.all_courts.forEach(court => {
                const key = `${court.facility_name}_${court.date}`;
                if (!groupedData[key]) {
                    groupedData[key] = {
                        facility_name: court.facility_name,
                        date: court.date,
                        courts: []
                    };
                }
                groupedData[key].courts.push(court);
            });

            let html = '<div class="table-responsive"><table class="table table-bordered status-table">';
            
            // 테이블 헤더
            html += '<thead><tr>';
            html += '<th>시설</th>';
            html += '<th>날짜</th>';
            html += '<th>전체 코트 수</th>';
            html += '<th>예약 가능</th>';
            html += '<th>예약 불가</th>';
            html += '</tr></thead><tbody>';

            // 시설명과 날짜로 정렬
            const sortedKeys = Object.keys(groupedData).sort((a, b) => {
                const [facilityA, dateA] = a.split('_');
                const [facilityB, dateB] = b.split('_');
                if (facilityA !== facilityB) return facilityA.localeCompare(facilityB);
                return dateA.localeCompare(dateB);
            });

            // 테이블 내용
            sortedKeys.forEach(key => {
                const group = groupedData[key];
                const totalCourts = group.courts.length;
                const availableCourts = group.courts.filter(court => {
                    return data.results.some(r => 
                        r.date === court.date && 
                        r.facility_name === court.facility_name && 
                        r.court === court.court && 
                        r.time === court.time
                    );
                }).length;
                const unavailableCourts = totalCourts - availableCourts;

                html += `<tr>
                    <td>${group.facility_name}</td>
                    <td>${group.date}</td>
                    <td>${totalCourts}</td>
                    <td class="status-available">${availableCourts}</td>
                    <td>${unavailableCourts}</td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            
            // 전체 통계
            const totalAllCourts = data.all_courts.length;
            html += `<div class="last-update">전체 코트 수: ${totalAllCourts}개</div>`;
            
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
                                // 탄천실내가 맨 위로 오도록 정렬
                                const facilityOrder = (a, b) => {
                                    if (a === '탄천실내') return -1;
                                    if (b === '탄천실내') return 1;
                                    return a.localeCompare(b, 'ko');
                                };
                                let html = '';
                                for (const [facility, dates] of Object.entries(facilities).sort((a, b) => facilityOrder(a[0], b[0]))) {
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
                            // 탄천실내가 맨 위로 오도록 정렬
                            const facilityOrder = (a, b) => {
                                if (a === '탄천실내') return -1;
                                if (b === '탄천실내') return 1;
                                return a.localeCompare(b, 'ko');
                            };
                            let html = '';
                            for (const [facility, dates] of Object.entries(facilities).sort((a, b) => facilityOrder(a[0], b[0]))) {
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
    """현재 모니터링 결과 반환 (캐시된 데이터 사용)"""
    global monitoring_results, all_courts_cache, last_update_time, cache_lock
    
    try:
        # 스레드 안전하게 캐시 읽기
        with cache_lock:
            # 캐시 데이터 복사 (Lock 내에서 빠르게 복사)
            import copy
            results_copy = copy.deepcopy(monitoring_results)
            all_courts_copy = copy.deepcopy(all_courts_cache)
            last_update_copy = last_update_time
        
        # 캐시된 데이터 반환
        response_data = {
            'results': results_copy,
            'all_courts': all_courts_copy,
            'last_update': last_update_copy.strftime('%Y-%m-%d %H:%M:%S') if last_update_copy else datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            'status': 'success' if (results_copy or all_courts_copy) else 'no_data'
        }
        
        # ensure_ascii=False로 한글 인코딩 문제 방지
        import json
        from flask import Response
        json_str = json.dumps(response_data, ensure_ascii=False, indent=2)
        return Response(json_str, mimetype='application/json; charset=utf-8')
        
    except Exception as e:
        print(f"❌ API 요청 처리 중 오류: {e}")
        import traceback
        traceback.print_exc()
        # 오류 발생 시에도 기본 응답 반환
        return jsonify({
            'results': [],
            'all_courts': [],
            'last_update': datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
            'status': 'error',
            'error_message': str(e)
        })



def send_telegram_notification(available_courts):
    """예약 가능한 코트가 있을 때 텔레그램 메시지 전송"""
    try:
        print(f"\n� 텔레그램 알림 전송 시작 - {len(available_courts)}개 코트")
        
        # 텔레그램 설정
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        
        print(f"📱 봇 토큰: {bot_token[:20] if bot_token else '없음'}...")
        print(f"� 채팅 ID: {chat_id}")
        
        if not bot_token or not chat_id:
            print("⚠️ 텔레그램 설정이 없습니다. telegram_config.txt 파일을 확인해주세요.")
            return
        
        if bot_token == "your_bot_token_here" or chat_id == "your_chat_id_here":
            print("⚠️ 기본값이 설정되어 있습니다. telegram_config.txt에서 실제 값으로 변경해주세요.")
            return
        
        # 메시지 내용 생성 (Markdown 형식)
        message = "🎾 *예약 가능한 테니스 코트 발견\!*\n\n"
        
        # 날짜별로 그룹화
        by_date = {}
        for court in available_courts:
            date = court['date']
            if date not in by_date:
                by_date[date] = []
            by_date[date].append(court)
        
        # 날짜별로 정렬하여 출력
        for date in sorted(by_date.keys()):
            # 날짜를 이스케이프 처리 (Markdown V2 형식)
            escaped_date = date.replace('-', '\\-').replace('.', '\\.')
            message += f"📅 *{escaped_date}*\n"
            
            # 시설별로 그룹화
            by_facility = {}
            for court in by_date[date]:
                facility = court['facility_name']
                if facility not in by_facility:
                    by_facility[facility] = []
                by_facility[facility].append(court)
            
            # 시설별로 정렬하여 출력
            for facility in sorted(by_facility.keys()):
                # 시설명을 이스케이프 처리
                escaped_facility = facility.replace('-', '\\-').replace('.', '\\.').replace('(', '\\(').replace(')', '\\)')
                message += f"🏟️ _{escaped_facility}_\n"
                for court in sorted(by_facility[facility], key=lambda x: x['time']):
                    # 코트 정보를 이스케이프 처리
                    escaped_court = court['court'].replace('-', '\\-').replace('.', '\\.').replace('(', '\\(').replace(')', '\\)')
                    escaped_time = court['time'].replace('-', '\\-').replace('.', '\\.').replace(':', '\\:').replace('~', '\\~')
                    message += f"  • {escaped_court} \\- {escaped_time}\n"
            message += "\n"
        
        message += "[예약하러 가기](https://res\\.isdc\\.co\\.kr/)\n\n"
        message += "_이 메시지는 자동으로 발송되었습니다\\._"
        
        print("� 메시지 내용 생성 완료")
        
        # 텔레그램 API 호출
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'MarkdownV2',
            'disable_web_page_preview': False
        }
        
        print(f"� 텔레그램 API 호출 중...")
        response = requests.post(url, json=payload, timeout=10, verify=False)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('ok'):
                print(f"✅ 텔레그램 알림 전송 완료")
            else:
                print(f"❌ 텔레그램 API 오류: {result.get('description', '알 수 없는 오류')}")
        else:
            print(f"❌ 텔레그램 API 호출 실패: HTTP {response.status_code}")
            print(f"   응답: {response.text}")
        
    except Exception as e:
        print(f"❌ 텔레그램 알림 전송 실패: {e}")
        import traceback
        traceback.print_exc()

def check_and_send_notification(available_results):
    """예약 가능한 코트를 확인하고 텔레그램 알림 전송 (부분 실패 상황에도 대응)"""
    try:
        print(f"\n🔍 예약 가능 알림 확인 시작 - 전체 예약 가능 코트 수: {len(available_results)}")
        
        # available_results가 비어있으면 종료
        if not available_results:
            print("📋 현재 조회된 예약 가능 코트가 없습니다")
            return
        
        # 현재 시간 확인 (12:00 AM ~ 07:00 AM 사이에는 이메일 전송 안함)
        current_time = datetime.now(KST)
        current_hour = current_time.hour
        
        # 12:00 AM (0시) ~ 07:01 AM (7시 1분) 사이인지 확인
        if 0 <= current_hour < 7 or (current_hour == 7 and current_time.minute == 0):
            print(f"⏰ 현재 시간: {current_time.strftime('%H:%M')} - 12:00 AM ~ 07:01 AM 시간대이므로 알림 전송을 건너뜁니다.")
            return
        
        # 탄천실내, 수내, 야탑에서 예약 가능한 코트 필터링
        target_facilities = ['탄천실내', '수내', '야탑', '구미']
        target_courts = []
        
        print("🎯 타겟 시설 예약 가능 코트 확인:")
        for result in available_results:
            print(f"  - {result['facility_name']} {result['court']} - {result['date']} {result['time']}")
            if any(facility in result['facility_name'] for facility in target_facilities):
                target_courts.append(result)
                print(f"    ✅ 타겟 시설 발견: {result['facility_name']}")
        
        print(f"🎯 타겟 시설 예약 가능 코트 수: {len(target_courts)}")
        
        if target_courts:
            # 현재 예약 가능한 코트 정보를 문자열로 변환하여 비교용 키 생성
            current_courts_key = ""
            for court in sorted(target_courts, key=lambda x: (x['date'], x['facility_name'], x['court'], x['time'])):
                current_courts_key += f"{court['facility_name']}_{court['court']}_{court['date']}_{court['time']}|"
            
            notification_key = current_time.strftime('%Y-%m-%d')
            
            print(f"📅 현재 날짜 키: {notification_key}")
            print(f"� 마지막 알림 전송 기록: {last_email_sent}")
            print(f"� 이전 예약 가능 코트 정보: {last_available_courts.get(notification_key, '없음')}")
            print(f"� 현재 예약 가능 코트 정보: {current_courts_key}")
            
            # 새로운 날짜인 경우
            if notification_key not in last_email_sent:
                print("� 새로운 날짜 - 알림 전송 시작")
                try:
                    send_telegram_notification(target_courts)
                    last_email_sent[notification_key] = current_time - timedelta(seconds=1800)  # 30분 전으로 설정하여 즉시 재전송 가능
                    last_available_courts[notification_key] = current_courts_key
                    print(f"✅ 알림 전송 완료: {len(target_courts)}개 코트")
                except Exception as e:
                    print(f"❌ 알림 전송 실패: {e}")
                    # 알림 전송 실패해도 프로그램은 계속 실행
            else:
                # 같은 날에 이미 알림을 보냈으면 1시간 후에 다시 보낼 수 있도록
                time_diff = current_time - last_email_sent[notification_key]
                print(f"⏰ 마지막 전송으로부터 경과 시간: {time_diff.total_seconds()}초")

                if time_diff.total_seconds() > 1800:  # 30분
                    # 이전 예약 가능한 코트 정보와 현재 정보 비교
                    previous_courts_key = last_available_courts.get(notification_key, "")
                    
                    if current_courts_key != previous_courts_key:
                        print("� 1시간 경과 + 내용 변동 - 알림 재전송 시작")
                        try:
                            send_telegram_notification(target_courts)
                            last_email_sent[notification_key] = current_time
                            last_available_courts[notification_key] = current_courts_key
                            print(f"✅ 알림 재전송 완료: {len(target_courts)}개 코트")
                        except Exception as e:
                            print(f"❌ 알림 재전송 실패: {e}")
                            # 알림 전송 실패해도 프로그램은 계속 실행
                    else:
                        print("⏳ 1시간 경과했지만 내용 변동 없음 - 알림 전송 건너뜀")
                else:
                    print("⏳ 1시간 미경과 - 알림 전송 건너뜀")
        else:
            print("❌ 타겟 시설에서 예약 가능한 코트가 없습니다.")
        
    except Exception as e:
        print(f"❌ 이메일 확인 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()

def run_flask():
    """Flask 서버 실행"""
    app.run(host='0.0.0.0', port=5000, debug=False)

def load_email_config():
    """email_config.txt 파일에서 이메일 설정을 로드하여 환경 변수에 설정"""
    email_config_file = "email_config.txt"
    if os.path.exists(email_config_file):
        try:
            with open(email_config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # 주석과 빈 줄 건너뛰기
                    if not line or line.startswith('#'):
                        continue
                    # KEY=VALUE 형식 파싱
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        # 기본값이 아닌 경우에만 환경 변수 설정
                        if value and value not in ['your_email@gmail.com', 'your_app_password', 'receiver1@gmail.com,receiver2@gmail.com']:
                            os.environ[key] = value
                            if key == 'EMAIL_PASSWORD':
                                print(f"✅ {key} 설정됨: ****")
                            else:
                                print(f"✅ {key} 설정됨: {value}")
        except Exception as e:
            print(f"❌ email_config.txt 파일 읽기 오류: {e}")

def load_telegram_config():
    """telegram_config.txt 파일에서 텔레그램 설정을 로드하여 환경 변수에 설정"""
    telegram_config_file = "telegram_config.txt"
    if os.path.exists(telegram_config_file):
        try:
            with open(telegram_config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # 주석과 빈 줄 건너뛰기
                    if not line or line.startswith('#'):
                        continue
                    # KEY=VALUE 형식 파싱
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        # 기본값이 아닌 경우에만 환경 변수 설정
                        if value and value not in ['your_bot_token_here', 'your_chat_id_here']:
                            os.environ[key] = value
                            if key == 'TELEGRAM_BOT_TOKEN':
                                print(f"✅ {key} 설정됨: {value[:20]}...")
                            else:
                                print(f"✅ {key} 설정됨: {value}")
        except Exception as e:
            print(f"❌ telegram_config.txt 파일 읽기 오류: {e}")
    else:
        print("⚠️ telegram_config.txt 파일이 없습니다. 텔레그램 알림을 사용하려면 파일을 생성해주세요.")

def load_accounts():
    """계정 정보를 로드하는 함수"""
    accounts = []
    
    # auth.txt 파일에서 계정 정보 로드 시도
    if os.path.exists("auth.txt"):
        try:
            with open("auth.txt", 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines() if line.strip()]
                
                # 한 줄에 username,password 형식 또는 두 줄씩 쌍으로 처리
                i = 0
                while i < len(lines):
                    line = lines[i]
                    
                    # 쉼표로 구분된 경우 (username,password)
                    if ',' in line:
                        parts = line.split(',')
                        if len(parts) >= 2:
                            username = parts[0].strip()
                            password = parts[1].strip()
                            if username and password:
                                accounts.append({'username': username, 'password': password})
                                print(f"✅ 계정 로드: {username}")
                        i += 1
                    # 두 줄씩 쌍으로 처리 (첫 줄: username, 둘째 줄: password)
                    elif i + 1 < len(lines):
                        username = lines[i].strip()
                        password = lines[i + 1].strip()
                        if username and password:
                            accounts.append({'username': username, 'password': password})
                            print(f"✅ 계정 로드: {username}")
                        i += 2
                    else:
                        i += 1
                        
        except Exception as e:
            print(f"❌ auth.txt 파일 읽기 오류: {e}")
    
    # auth.txt에서 로드하지 못한 경우 환경 변수에서 로드
    if not accounts:
        # 기본 환경 변수 (WebId, WebPassword)
        username = os.environ.get("WebId")
        password = os.environ.get("WebPassword")
        if username and password:
            accounts.append({'username': username, 'password': password})
            print(f"✅ 환경변수에서 계정 로드: {username}")
        
        # 다중 계정 환경 변수 (WebId1, WebPassword1, WebId2, WebPassword2, ...)
        for i in range(1, 4):  # 최대 3개 계정
            username = os.environ.get(f"WebId{i}")
            password = os.environ.get(f"WebPassword{i}")
            if username and password:
                accounts.append({'username': username, 'password': password})
                print(f"✅ 환경변수에서 계정 로드: {username}")
    
    return accounts

def main():
    try:
        # 이메일 설정 로드 (호환성 유지)
        load_email_config()
        
        # 텔레그램 설정 로드
        load_telegram_config()
        
        # 다중 계정 정보 로드
        accounts = load_accounts()
        
        if not accounts:
            print("❌ 인증 정보를 찾을 수 없습니다.")
            print("다음 중 하나의 방법으로 계정 정보를 설정해주세요:")
            print("1. auth.txt 파일에 다음 형식으로 저장:")
            print("   username1,password1")
            print("   username2,password2")
            print("   username3,password3")
            print("   또는")
            print("   username1")
            print("   password1")
            print("   username2")
            print("   password2")
            print("   username3")
            print("   password3")
            print("2. 환경 변수 설정:")
            print("   WebId1, WebPassword1")
            print("   WebId2, WebPassword2")
            print("   WebId3, WebPassword3")
            return
        
        print(f"✅ 총 {len(accounts)}개의 계정이 로드되었습니다.")
        
        # 웹 인터페이스 설정
        create_templates_dir()
        create_static_dir()
        create_html_template()
        create_css_file()
        
        # 스케줄러 초기화 (다중 계정 전달)
        global scheduler
        scheduler = TennisCourtScheduler(accounts)
        
        # Flask 서버를 별도 스레드로 실행
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()
        
        print("🌐 웹 서버가 시작되었습니다. http://localhost:5000 에서 확인하세요.")
        
        # 모니터링 실행
        monitoring_count = 0
        while True:
            try:
                # 모니터링 실행
                results, all_courts = scheduler.monitor_courts()
                
                # 결과 업데이트 (전역 캐시에 병합)
                global monitoring_results, all_courts_cache, last_update_time, cache_lock
                
                # 스레드 안전하게 캐시 업데이트
                with cache_lock:
                    # 예약 가능한 코트는 항상 최신 데이터로 교체
                    monitoring_results = results
                    
                
                # 전체 코트 캐시 업데이트: 이번 사이클에서 조회한 데이터로 완전 교체
                # (캐시 병합 제거 - 매 사이클마다 모든 4일치를 조회하므로 병합 불필요)
                if all_courts:
                    all_courts_cache = all_courts[:]
                else:
                    # 새 데이터가 없으면 기존 캐시 유지
                    print(f"\n💾 새 데이터 없음 - 기존 캐시 유지: {len(all_courts_cache)}개")
                
                last_update_time = datetime.now(KST)                # 알림 확인 및 전송 (부분 실패 상황에서도 성공한 데이터가 있으면 전송)
                try:
                    check_and_send_notification(results)
                except Exception as e:
                    print(f"❌ 알림 확인/전송 중 오류: {e}")
                    import traceback
                    traceback.print_exc()
                    # 알림 전송 실패해도 모니터링은 계속
                
                # 5번 모니터링마다 계정 순환 (약 5분마다)
                monitoring_count += 1
                if monitoring_count % 5 == 0 and len(accounts) > 1:
                    print(f"\n🔄 정기 계정 순환 (모니터링 {monitoring_count}회)")
                    scheduler.switch_to_next_account()
                
                # 1분 대기
                print(f"\n⏰ 다음 모니터링까지 90초 대기...")
                time.sleep(90)
                
            except Exception as e:
                print(f"❌ 모니터링 루프 중 오류 발생: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(90)  # 오류 발생 시 90초 대기
        
    except Exception as e:
        print(f"❌ 프로그램 실행 중 오류 발생: {e}")

if __name__ == "__main__":
    main()
