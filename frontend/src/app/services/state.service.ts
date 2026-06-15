import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject, timer } from 'rxjs';
import { State } from '../models/state.model';
import { SupervisorState } from '../models/supervisor_state.model';
import { switchMap, catchError } from 'rxjs/operators';
import { of } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class StateService {
  private stateSubject = new BehaviorSubject<State | null>(null);
  state$ = this.stateSubject.asObservable();
  private supervisorStateSubject = new BehaviorSubject<SupervisorState | null>(null);
  supervisorState$ = this.supervisorStateSubject.asObservable();

  constructor(private http: HttpClient) {
    // Poll state every 2 seconds
    timer(0, 2000).pipe(
      switchMap(() => this.http.get<State>('/state.json').pipe(
        catchError(err => {
          console.error('Error fetching state:', err);
          console.error('Status:', err.status);
          console.error('Error body:', err.error);  // This will show what actually came back
          return of(null);
        })
      ))
    ).subscribe(state => {
      if (state) this.stateSubject.next(state);
    });

    // Poll supervisor state every 5 seconds
    timer(0, 5000).pipe(
      switchMap(() => this.http.get<SupervisorState>('/supervisor_state.json').pipe(
        catchError(err => {
          console.error('Error fetching supervisor state:', err);
          return of(null);
        })
      ))
    ).subscribe(supervisorState => {
      if (supervisorState) this.supervisorStateSubject.next(supervisorState);
    });
  }

  fetchState() {
    this.http.get<State>('/state.json').subscribe(state => {
      this.stateSubject.next(state);
    });
  }

  fetchSupervisorState() {
    this.http.get<SupervisorState>('/supervisor_state.json').subscribe(supervisorState => {
      this.supervisorStateSubject.next(supervisorState);
    });
  }
}