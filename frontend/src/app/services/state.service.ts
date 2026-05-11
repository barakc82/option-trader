import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject } from 'rxjs';
import { State } from '../models/state.model';
import { Observable } from 'rxjs';
import { SupervisorState } from '../models/supervisor_state.model';

@Injectable({ providedIn: 'root' })
export class StateService {
  private stateSubject = new BehaviorSubject<State | null>(null);
  state$ = this.stateSubject.asObservable();
  private supervisorStateSubject = new BehaviorSubject<SupervisorState | null>(null);
  supervisorState$ = this.supervisorStateSubject.asObservable();
  
  constructor(private http: HttpClient) {}

  fetchState() {
    this.http.get<State>('/api/state').subscribe(state => {
      this.stateSubject.next(state);
    });
  }

  fetchSupervisorState() {
    this.http.get<State>('/api/supervisor-state').subscribe(supervisorState => {
      this.supervisorStateSubject.next(supervisorState);
    });
  }
}