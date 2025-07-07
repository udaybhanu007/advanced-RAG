package com.example.springbootapp.dao;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;
import com.example.springbootapp.model.ExampleModel;

@Repository
public interface ExampleDao extends JpaRepository<ExampleModel, Long> {
    // No need to redeclare inherited methods; use JpaRepository's methods directly.
}
