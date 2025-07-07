package com.example.springbootapp.repository;

import org.springframework.data.jpa.repository.JpaRepository;

import com.example.springbootapp.model.ExampleModel;
import org.springframework.stereotype.Repository;

@Repository
public interface ExampleRepository extends JpaRepository<ExampleModel, Long> {
	// No changes needed, as ExampleModel is used directly.
}
