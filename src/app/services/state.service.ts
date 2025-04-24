import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { State } from '../models/state.model';
import { Observable } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class StateService {
  constructor(private http: HttpClient) {}

  getState(): Observable<State> {
    return this.http.get<State>('/api/state');
  }
}